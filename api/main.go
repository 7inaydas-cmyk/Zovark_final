package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"runtime/debug"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	apihandlers "github.com/hydra-platform/hydra-api/handlers"
)

// safeGoroutine runs fn in a new goroutine with a panic-recover. If fn panics,
// the goroutine logs the panic (with stack) instead of crashing the whole API
// process. Use for all fire-and-forget background work — audit writes, pushback,
// apikey stamp updates, materialized-view refreshes — where a nil dbPool during
// shutdown or a pgx bug would otherwise take the server down.
func safeGoroutine(name string, fn func(ctx context.Context)) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("[safeGoroutine:%s] panic recovered: %v\n%s", name, r, debug.Stack())
			}
		}()
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		fn(ctx)
	}()
}

// initSSELimits sets sseMaxSubscribers from ZOVARK_SSE_MAX_SUBSCRIBERS env
// (default min(pool/3, 200)). Called once from main() after initDB.
func initSSELimits() {
	if raw := strings.TrimSpace(os.Getenv("ZOVARK_SSE_MAX_SUBSCRIBERS")); raw != "" {
		if n, err := strconv.ParseInt(raw, 10, 64); err == nil && n > 0 {
			atomic.StoreInt64(&sseMaxSubscribers, n)
			return
		}
	}
	// Try to derive from pool capacity.
	if dbPool != nil {
		poolCap := dbPool.Config().MaxConns
		calc := int64(poolCap) / 3
		if calc < 8 {
			calc = 8
		}
		if calc > 200 {
			calc = 200
		}
		atomic.StoreInt64(&sseMaxSubscribers, calc)
	}
}

var (
	appConfig *Config
	startTime = time.Now()
)

type Config struct {
	Port            string
	DatabaseURL     string
	TemporalAddress string
	LLMKey          string
	JWTSecret       string
	// OIDC/SSO configuration
	OIDCIssuerURL    string
	OIDCClientID     string
	OIDCClientSecret string
	OIDCRedirectURI  string
	OIDCRoleClaimKey string
	// Vault
	VaultAddr  string
	VaultToken string
	// Redis
	RedisURL string
}

func init() {
	appConfig = &Config{
		Port:            getEnvOrDefault("PORT", "8090"),
		DatabaseURL:     getEnvOrDefault("DATABASE_URL", "postgresql://zovark:hydra_dev_2026@postgres:5432/zovark"), // FIX #17
		TemporalAddress: getEnvOrDefault("TEMPORAL_ADDRESS", "temporal:7233"),
		LLMKey:          getEnvOrDefault("ZOVARK_LLM_KEY", ""),
		JWTSecret:       getEnvOrDefault("JWT_SECRET", ""),
		// OIDC
		OIDCIssuerURL:    getEnvOrDefault("OIDC_ISSUER_URL", ""),
		OIDCClientID:     getEnvOrDefault("OIDC_CLIENT_ID", ""),
		OIDCClientSecret: getEnvOrDefault("OIDC_CLIENT_SECRET", ""),
		OIDCRedirectURI:  getEnvOrDefault("OIDC_REDIRECT_URI", "http://localhost:8090/api/v1/auth/callback"),
		OIDCRoleClaimKey: getEnvOrDefault("OIDC_ROLE_CLAIM_KEY", "role"),
		// Vault
		VaultAddr:  getEnvOrDefault("VAULT_ADDR", ""),
		VaultToken: getEnvOrDefault("VAULT_TOKEN", ""),
		// Redis
		RedisURL: getEnvOrDefault("REDIS_URL", "redis:6379"),
	}
}

func getEnvOrDefault(key, fallback string) string {
	if value, exists := os.LookupEnv(key); exists {
		return value
	}
	return fallback
}

func main() {
	// Check for CLI commands
	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		cmd := "up"
		if len(os.Args) > 2 {
			cmd = os.Args[2]
		}
		// Initialize DB for migration commands
		initVault()
		defer stopVault()
		appConfig.DatabaseURL = GetSecret("database_url", "DATABASE_URL", appConfig.DatabaseURL)
		appConfig.JWTSecret = GetSecret("jwt_secret", "JWT_SECRET", appConfig.JWTSecret)
		if err := initDB(appConfig.DatabaseURL); err != nil {
			log.Fatalf("Failed to initialize database: %v", err)
		}
		defer closeDB()
		runMigrations(cmd, os.Args[3:])
		return
	}

	log.Println("Starting Zovark API Gateway...")

	// Initialize Vault (for secrets management — must come before DB init)
	initVault()
	defer stopVault()

	// Override config from Vault if available
	appConfig.DatabaseURL = GetSecret("database_url", "DATABASE_URL", appConfig.DatabaseURL)
	appConfig.JWTSecret = GetSecret("jwt_secret", "JWT_SECRET", appConfig.JWTSecret)

	// Enforce strong JWT secret
	if len(appConfig.JWTSecret) < 32 {
		log.Fatal("FATAL: JWT_SECRET must be at least 32 characters. Generate with: openssl rand -base64 64")
	}

	// OpenTelemetry before DB/Redis so pgx tracer and redisotel see a real TracerProvider (Ticket 9).
	initAPIOTel(context.Background())
	defer func() {
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		shutdownAPIOTel(sctx)
	}()

	// Initialize Database connection
	err := initDB(appConfig.DatabaseURL)
	if err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	defer closeDB()

	// Configure SSE subscriber cap after pool is ready (task 1.8).
	initSSELimits()

	// Initialize Redis for rate limiting
	initRedis()
	instrumentRedisOTel()

	// Initialize Temporal client
	err = initTemporal(appConfig.TemporalAddress)
	if err != nil {
		log.Fatalf("Failed to initialize Temporal client: %v", err)
	}
	defer closeTemporal()

	// Initialize OIDC (SSO)
	initOIDC()

	// Redpanda writer for tasks.new.{tenant} (canonical dispatch)
	initRedpandaWriter()
	defer closeRedpandaWriter()

	// Start backpressure queue drain goroutine
	drainCtx, drainCancel := context.WithCancel(context.Background())
	defer drainCancel()
	go startQueueDrainLoop(drainCtx)

	// Audit 1.16: start the auth rate-limiter sweeper so the in-memory attempts
	// map doesn't grow unbounded with stale IPs.
	safeGoroutine("authLimiter.sweeper", func(ctx context.Context) {
		<-drainCtx.Done() // tie sweeper lifetime to drain goroutine
	})
	go startAuthLimiterSweeper(drainCtx)

	// Setup Gin router
	router := gin.Default()

	// Middlewares (OTEL first — propagates trace context into handlers / Redpanda publish)
	router.Use(otelGinMiddleware())
	router.Use(zovarkTraceHeaderMiddleware())
	router.Use(otelHTTPMetricsMiddleware())
	router.Use(corsMiddleware())
	router.Use(securityHeadersMiddleware())
	router.Use(loggingMiddleware())
	router.Use(maxBodySizeMiddleware(10 << 20)) // 10MB global limit

	// Public routes
	router.GET("/health", healthCheckHandler)
	router.GET("/ready", readinessHandler)

	// Public auth routes (rate limited)
	auth := router.Group("/api/v1/auth")
	auth.Use(authRateLimitMiddleware())
	{
		auth.POST("/login", loginHandler)
		auth.POST("/register", registerHandler)
		auth.POST("/refresh", refreshHandler)
		auth.POST("/logout", logoutHandler)
		// SSO/OIDC routes (public)
		auth.GET("/sso/login", ssoLoginHandler)
		auth.GET("/callback", ssoCallbackHandler)
	}

	// Public webhook route (HMAC-validated, no JWT)
	router.POST("/api/v1/webhooks/:source_id/alert", webhookAlertHandler)

	// Platform training-data gateway (Ticket 5): HTTPS Bearer → Redis rate:ingest:* → Redpanda raw.training-data.{customer_id} → 204
	platformTraining := router.Group("/api/v1/platform/training-data")
	{
		platformTraining.POST("/ingest", apihandlers.PlatformTrainingIngest(apihandlers.PlatformIngestDeps{
			BearerSecret: strings.TrimSpace(os.Getenv("ZOVARK_PLATFORM_INGEST_BEARER")),
			Redis:        redisClient,
			Publish: func(ctx context.Context, customerID string, body []byte) error {
				return publishRawTrainingData(ctx, customerID, body)
			},
		}))
	}

	// Protected API routes
	api := router.Group("/api/v1")
	api.Use(authMiddleware())
	api.Use(tenantRateLimitMiddleware())
	api.Use(auditMiddleware())
	{
		// SSE global stream (Mission 9) — also accepts ?token= for EventSource
		api.GET("/tasks/stream", streamAllTaskUpdates)

		// Anyone authenticated
		api.GET("/tasks", listTasksHandler)
		api.GET("/tasks/:id", getTaskHandler)
		api.GET("/tasks/:id/audit", getTaskAuditHandler)
		api.GET("/tasks/:id/steps", getTaskStepsHandler)
		api.GET("/tasks/:id/timeline", getTaskTimelineHandler)
		api.GET("/tasks/:id/stream", taskSSEHandler)
		api.GET("/entities/:id/neighborhood", entityNeighborhoodHandler)
		api.GET("/entities", listEntityGraphHandler)
		api.GET("/stats", getStatsHandler)
		api.GET("/playbooks", listPlaybooksHandler)
		api.GET("/skills", listSkillsHandler)
		api.GET("/me", getMeHandler)
		api.GET("/log-sources", listLogSourcesHandler)
		api.GET("/siem-alerts", listSIEMalertsHandler)
		api.GET("/notifications", getNotificationsHandler)

		// Analyst + Admin (LLM-triggering endpoints have token quota check)
		api.POST("/tasks", requireRole("admin", "analyst", "api_key"), checkTokenQuota(), createTaskHandler)
		api.POST("/tasks/bulk", requireRole("admin", "analyst", "api_key"), checkTokenQuota(), bulkCreateTasksHandler)
		api.POST("/tasks/upload", requireRole("admin", "analyst"), checkTokenQuota(), uploadTaskHandler)
		api.POST("/siem-alerts/:id/investigate", requireRole("admin", "analyst"), checkTokenQuota(), investigateAlertHandler)
		api.POST("/investigations/:id/feedback", requireRole("admin", "analyst"), submitFeedbackHandler)

		// TOTP 2FA (authenticated users)
		api.POST("/auth/totp/setup", totpSetupHandler)
		api.POST("/auth/totp/verify", totpVerifyHandler)

		// Sandbox execution (DPO pipeline)
		api.POST("/sandbox/execute", requireRole("admin", "analyst"), sandboxExecuteHandler)

		// Feedback stats (admin only)
		api.GET("/feedback/stats", requireRole("admin"), getFeedbackStatsHandler)

		// Analytics — feedback insights (all tenant-scoped)
		api.GET("/analytics/feedback/summary", feedbackSummaryHandler)
		api.GET("/analytics/feedback/rules", feedbackRulesHandler)
		api.GET("/analytics/feedback/analysts", feedbackAnalystsHandler)

		// Webhook endpoints (authenticated)
		api.GET("/webhooks/endpoints", listWebhookEndpointsHandler)
		api.GET("/webhooks/deliveries", listWebhookDeliveriesHandler)

		// Audit export (admin only)
		api.GET("/audit/export", requireRole("admin"), auditExportHandler)

		// Diagnostic export — Flight Data Recorder (Mission 6)
		api.GET("/admin/diagnostics/export", requireRole("admin"), diagnosticExportHandler)
		api.POST("/admin/diagnostics/probe-db", requireRole("admin"), probeDBHandler)

		// Compliance Evidence Engine (Mission 7)
		api.POST("/compliance/report/:framework", requireRole("admin", "analyst"), complianceReportHandler)

		// API key management (admin only)
		api.POST("/api-keys", requireRole("admin"), createAPIKeyHandler)
		api.GET("/api-keys", requireRole("admin"), listAPIKeysHandler)
		api.DELETE("/api-keys/:id", requireRole("admin"), deleteAPIKeyHandler)

		api.POST("/mcp-keys", requireRole("admin"), createMCPKeyHandler)
		api.GET("/mcp-keys", requireRole("admin"), listMCPKeysHandler)
		api.DELETE("/mcp-keys/:id", requireRole("admin"), revokeMCPKeyHandler)

		// Admin only — DB-backed task-level approvals (Temporal signal gate)
		api.GET("/approvals/pending", requireRole("admin"), getPendingApprovalsHandler)
		api.POST("/approvals/:id/decide", requireRole("admin"), decideApprovalHandler)

		// MCP human-in-the-loop approval gate — Redis-backed workflow approvals
		// Any authenticated caller may request an approval or check its status.
		api.POST("/mcp/approvals/request", requestMCPApprovalHandler)
		api.GET("/mcp/approvals/check/:token", checkMCPApprovalHandler)
		// Admin-only: list, lookup, and decide on pending approvals.
		api.GET("/mcp/approvals/pending", requireRole("admin"), listMCPApprovalsHandler)
		api.GET("/mcp/approvals/id/:approval_id", requireRole("admin"), getMCPApprovalByIDHandler)
		api.POST("/mcp/approvals/:token/decide", requireRole("admin"), decideMCPApprovalHandler)

		api.POST("/playbooks", requireRole("admin"), createPlaybookHandler)
		api.PUT("/playbooks/:id", requireRole("admin"), updatePlaybookHandler)
		api.DELETE("/playbooks/:id", requireRole("admin"), deletePlaybookHandler)
		api.POST("/log-sources", requireRole("admin"), createLogSourceHandler)
		api.PUT("/log-sources/:id", requireRole("admin"), updateLogSourceHandler)
		api.DELETE("/log-sources/:id", requireRole("admin"), deleteLogSourceHandler)

		// Tenant management (admin only)
		api.GET("/tenants", requireRole("admin"), listTenantsHandler)
		api.GET("/tenants/:id", requireRole("admin"), getTenantHandler)
		api.POST("/tenants", requireRole("admin"), createTenantHandler)
		api.PUT("/tenants/:id", requireRole("admin"), updateTenantHandler)
		api.DELETE("/tenants/:id/data", requireRole("admin"), gdprEraseHandler)

		// Webhook endpoint management (admin only)
		api.POST("/webhooks/endpoints", requireRole("admin"), createWebhookEndpointHandler)
		api.PUT("/webhooks/endpoints/:id", requireRole("admin"), updateWebhookEndpointHandler)
		api.DELETE("/webhooks/endpoints/:id", requireRole("admin"), deleteWebhookEndpointHandler)

		// Model registry (admin only)
		api.GET("/models", requireRole("admin"), listModelsHandler)
		api.POST("/models", requireRole("admin"), createModelHandler)
		api.PUT("/models/:id", requireRole("admin"), updateModelHandler)

		// A/B testing (admin only)
		api.GET("/models/ab-tests", requireRole("admin"), listABTestsHandler)
		api.POST("/models/ab-tests", requireRole("admin"), createABTestHandler)
		api.POST("/models/ab-tests/:id/complete", requireRole("admin"), completeABTestHandler)

		// Data retention (admin only)
		api.GET("/retention-policies", requireRole("admin"), listRetentionPoliciesHandler)
		api.PUT("/retention-policies/:id", requireRole("admin"), updateRetentionPolicyHandler)

		// Integration management (admin only)
		api.POST("/integrations/slack/test", requireRole("admin"), testSlackWebhookHandler)
		api.PUT("/integrations/slack", requireRole("admin"), configureSlackWebhookHandler)
		api.POST("/integrations/teams/test", requireRole("admin"), testTeamsWebhookHandler)
		api.PUT("/integrations/teams", requireRole("admin"), configureTeamsWebhookHandler)

		// Shadow mode (authenticated users)
		api.GET("/shadow/recommendations", listShadowRecommendationsHandler)
		api.GET("/shadow/recommendations/:id", getShadowRecommendationHandler)
		api.POST("/shadow/recommendations/:id/decide", decideShadowRecommendationHandler)
		api.GET("/shadow/conformance", getShadowConformanceHandler)
		api.GET("/shadow/status", getShadowStatusHandler)

		// Automation controls (authenticated for GET, admin for mutations)
		api.GET("/automation/controls", listAutomationControlsHandler)
		api.POST("/automation/controls", requireRole("admin"), upsertAutomationControlHandler)
		api.POST("/automation/kill", requireRole("admin"), emergencyKillHandler)
		api.POST("/automation/resume", requireRole("admin"), resumeAutomationHandler)
		api.GET("/automation/audit", requireRole("admin"), getKillSwitchAuditHandler)

		// Token quotas (authenticated for GET, admin for mutations)
		api.GET("/quotas", getTokenQuotaHandler)
		api.PUT("/quotas", requireRole("admin"), updateTokenQuotaHandler)
		api.POST("/quotas/circuit-breaker", requireRole("admin"), circuitBreakerHandler)
		api.GET("/quotas/usage", getTokenUsageHandler)

		// Metrics (admin only)
		api.GET("/metrics", requireRole("admin"), metricsHandler)

		// Sprint 1K: Cross-tenant intelligence (authenticated users)
		api.GET("/intelligence/top-threats", topThreatsHandler)
		api.GET("/intelligence/stats", intelligenceStatsHandler)

		// Sprint 2A: Detection engine (authenticated for GET, admin for mutations)
		api.GET("/detections/rules", listDetectionRulesHandler)
		api.GET("/detections/stats", detectionStatsHandler)

		// Sprint 2B: SOAR response playbooks
		api.GET("/response/playbooks", listResponsePlaybooksHandler)
		api.POST("/response/playbooks", requireRole("admin"), createResponsePlaybookHandler)
		api.PUT("/response/playbooks/:id", requireRole("admin"), updateResponsePlaybookHandler)
		api.DELETE("/response/playbooks/:id", requireRole("admin"), deleteResponsePlaybookHandler)
		api.GET("/response/executions", listResponseExecutionsHandler)
		api.GET("/response/executions/:id", getResponseExecutionHandler)
		api.POST("/response/executions/:id/approve", requireRole("admin"), approveResponseExecutionHandler)
		api.POST("/response/executions/:id/rollback", requireRole("admin"), rollbackResponseExecutionHandler)

		// Cipher Audit (Zovark Sprint 2C)
		api.GET("/cipher-audit/stats", cipherAuditStatsHandler)
		api.GET("/cipher-audit/summary", cipherAuditSummaryHandler)
		api.GET("/cipher-audit/findings", cipherAuditFindingsHandler)
		api.GET("/cipher-audit/servers", cipherAuditServersHandler)
		api.POST("/cipher-audit/analyze", requireRole("admin", "analyst"), cipherAuditAnalyzeHandler)

		// Template Promotion Flywheel (Sprint 2D) + Quorum (Mission 2)
		api.GET("/promotion-queue", promotionQueueHandler)
		api.POST("/analyst-feedback", requireRole("admin", "analyst"), analystFeedbackHandler)
		api.POST("/promotion-approve", requireRole("admin", "analyst"), approvePromotionHandler)
		api.GET("/auto-templates", autoTemplatesHandler)
		api.DELETE("/auto-templates/:slug", requireRole("admin"), disableAutoTemplateHandler)
		api.GET("/dashboard-stats", dashboardStatsHandler)

		// Governance configuration (admin only)
		api.GET("/governance/config", requireRole("admin"), getGovernanceConfigHandler)
		api.PUT("/governance/config", requireRole("admin"), updateGovernanceConfigHandler)
	}

	// SIEM ingest routes — high-volume alert ingestion
	// Rate control handled by 3-layer burst protection (dedup + batch + backpressure)
	// NOT by per-tenant rate limiter — see HANDOVER.md
	siem := router.Group("/api/v1/ingest")
	siem.Use(authMiddleware())
	siem.Use(auditMiddleware())
	{
		siem.POST("/splunk", requireRole("admin", "analyst", "api_key"), checkTokenQuota(), splunkIngestHandler)
		siem.POST("/elastic", requireRole("admin", "analyst", "api_key"), checkTokenQuota(), elasticIngestHandler)
		siem.POST("/cef", requireRole("admin", "analyst", "api_key"), checkTokenQuota(), cefIngestHandler)
		siem.POST("/leef", requireRole("admin", "analyst", "api_key"), checkTokenQuota(), leefIngestHandler)
		siem.GET("/health", requireRole("admin", "analyst", "api_key"), ingestHealthHandler)
	}

	// Control Plane — admin routes for config, diagnostics, bootstrap
	adminGroup := router.Group("/api/v1/admin")
	adminGroup.Use(authMiddleware())
	adminGroup.Use(requireRole("admin"))
	adminGroup.Use(auditMiddleware())
	{
		// Diagnostics proxy (sidecar)
		adminGroup.POST("/diagnostics/ping", handleDiagPing)
		adminGroup.POST("/diagnostics/http-check", handleDiagHTTPCheck)
		adminGroup.POST("/diagnostics/dns", handleDiagDNS)
		adminGroup.POST("/diagnostics/tcp", handleDiagTCP)
		adminGroup.POST("/diagnostics/parse-test", handleDiagParseTest)
		adminGroup.GET("/diagnostics/health", handleDiagHealth)

		// System health (combined OOB + diagnostics)
		adminGroup.GET("/system/health", handleSystemHealth)
		adminGroup.GET("/health/deep", handleSystemHealth)

		// Config management
		adminGroup.GET("/config", handleConfigGetAll)
		adminGroup.GET("/config/audit", handleConfigAuditLog)
		adminGroup.GET("/config/:key", handleConfigGet)
		adminGroup.PUT("/config", handleConfigUpsert)
		adminGroup.DELETE("/config/:key", handleConfigDelete)
		adminGroup.POST("/config/:key/rollback/:audit_id", handleConfigRollback)

		// Bootstrap wizard
		adminGroup.POST("/bootstrap/inject-synthetic", handleInjectSynthetic)
	}

	// Break-glass emergency auth — NO auth middleware (it IS the auth)
	router.POST("/api/v1/admin/breakglass/login", handleBreakglassLogin)

	// Start OOB watchdog on :9091 (independent of main Gin server)
	// Wait for it to be listening before starting main Gin server.
	oobReady := make(chan struct{})
	go startOOBServer(oobReady)
	select {
	case <-oobReady:
		log.Println("[oob] watchdog ready")
	case <-time.After(5 * time.Second):
		log.Println("[oob] WARNING: watchdog did not become ready in 5s, proceeding anyway")
	}

	// Audit 1.1: graceful shutdown. Previously router.Run() blocked forever and
	// SIGTERM killed in-flight ingest inserts, Temporal publishes, and the
	// drain goroutine mid-work. We now:
	//   1. wrap the gin handler in an http.Server
	//   2. install a signal handler for SIGINT / SIGTERM
	//   3. on signal: cancel the drain goroutine (drainCancel) FIRST so it
	//      doesn't claim new rows during shutdown
	//   4. call server.Shutdown(ctx) with a deadline so in-flight HTTP
	//      requests can complete
	//   5. fall through to the existing defers (closeDB, closeTemporal,
	//      closeRedpandaWriter) after the server has drained
	srv := &http.Server{
		Addr:    ":" + appConfig.Port,
		Handler: router,
		// Modest timeouts — the audit SSE code assumes hours-long connections,
		// so we can't set a ReadTimeout or WriteTimeout here without breaking
		// the existing streams. A dedicated ReadHeaderTimeout is safe.
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	serverErrCh := make(chan error, 1)
	go func() {
		log.Printf("Listening and serving HTTP on %s\n", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			serverErrCh <- err
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	select {
	case err := <-serverErrCh:
		log.Fatalf("http.Server failed: %v", err)
	case sig := <-sigCh:
		log.Printf("[shutdown] signal received: %s", sig)
	}

	// Order matters. Stop claiming new work before draining HTTP.
	log.Println("[shutdown] stopping drain goroutine")
	drainCancel()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer shutdownCancel()

	log.Println("[shutdown] http.Server.Shutdown (30s deadline)")
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("[shutdown] server.Shutdown returned: %v", err)
	}

	log.Println("[shutdown] http server drained; running deferred cleanup")
	// Deferred closeDB / closeTemporal / closeRedpandaWriter run on main exit.
}
