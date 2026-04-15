package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"runtime"
	"time"

	"github.com/gin-gonic/gin"
)

// ============================================================
// DIAGNOSTIC EXPORT — Flight Data Recorder (Mission 6)
// ============================================================

var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(AKIA[0-9A-Z]{16})`),
	regexp.MustCompile(`(?i)(password|passwd|pwd)\s*[:=]\s*\S+`),
	regexp.MustCompile(`\b\d{3}-\d{2}-\d{4}\b`),
	regexp.MustCompile(`(?i)(sk|pk|api[_-]?key|token|secret|bearer)[_-]?\w{20,}`),
	regexp.MustCompile(`(?i)(hydra[_-]dev[_-]2026|hydra-redis-dev-2026|zovark[_-]dev[_-]2026|zovark-redis-dev-2026)`),
	// JWTs (eyJ...eyJ...base64url)
	regexp.MustCompile(`eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`),
	// GCP service account JSON private keys
	regexp.MustCompile(`"private_key"\s*:\s*"[^"]+"`),
	regexp.MustCompile(`-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----`),
	// Azure SAS tokens
	regexp.MustCompile(`(?i)\bsig=[A-Za-z0-9%]{20,}`),
	// Slack webhooks
	regexp.MustCompile(`https://hooks\.slack\.com/services/[A-Z0-9/]+`),
	// Stripe live/test keys
	regexp.MustCompile(`sk_(?:live|test)_[0-9a-zA-Z]{24,}`),
	regexp.MustCompile(`rk_(?:live|test)_[0-9a-zA-Z]{24,}`),
	// GitHub tokens
	regexp.MustCompile(`gh[pousr]_[0-9a-zA-Z]{20,}`),
	// Google API keys
	regexp.MustCompile(`AIza[0-9A-Za-z_-]{35}`),
}

func scrubSecrets(input string) string {
	result := input
	for _, pattern := range secretPatterns {
		result = pattern.ReplaceAllString(result, "[REDACTED]")
	}
	return result
}

// diagnosticExportHandler creates a .zvk (zip) diagnostic bundle.
// GET /api/v1/admin/diagnostics/export
func diagnosticExportHandler(c *gin.Context) {
	if !enforceAdminDiagnosticRateLimit(c) {
		return
	}
	tenantID := c.MustGet("tenant_id").(string)
	userID := c.GetString("user_id")
	ctx := c.Request.Context()

	buf := new(bytes.Buffer)
	zw := zip.NewWriter(buf)

	// 1. Audit events (last 5000)
	auditRows, err := dbPool.Query(ctx, `
		SELECT id, tenant_id, event_type, actor_type, resource_type, resource_id, metadata, created_at
		FROM audit_events WHERE tenant_id = $1
		ORDER BY created_at DESC LIMIT 5000
	`, tenantID)
	if err == nil {
		var audits []map[string]interface{}
		defer auditRows.Close()
		for auditRows.Next() {
			var id, eventType, actorType, resourceType string
			var tid, resourceID *string
			var metadata interface{}
			var createdAt time.Time
			if err := auditRows.Scan(&id, &tid, &eventType, &actorType, &resourceType, &resourceID, &metadata, &createdAt); err != nil {
				continue
			}
			audits = append(audits, map[string]interface{}{
				"id": id, "event_type": eventType, "actor_type": actorType,
				"resource_type": resourceType, "resource_id": resourceID,
				"metadata": metadata, "created_at": createdAt.Format(time.RFC3339),
			})
		}
		addJSONToZip(zw, "audit_events.json", audits)
	}

	// 2. LLM audit log (last 5000)
	llmRows, err := dbPool.Query(ctx, `
		SELECT id, task_id, stage, task_type, model_name, tokens_in, tokens_out,
		       latency_ms, status, error_message, created_at
		FROM llm_audit_log WHERE tenant_id = $1
		ORDER BY created_at DESC LIMIT 5000
	`, tenantID)
	if err == nil {
		var llmLogs []map[string]interface{}
		defer llmRows.Close()
		for llmRows.Next() {
			var id, stage, taskType, modelName, status string
			var taskID, errorMessage *string
			var tokensIn, tokensOut, latencyMs int
			var createdAt time.Time
			if err := llmRows.Scan(&id, &taskID, &stage, &taskType, &modelName,
				&tokensIn, &tokensOut, &latencyMs, &status, &errorMessage, &createdAt); err != nil {
				continue
			}
			llmLogs = append(llmLogs, map[string]interface{}{
				"id": id, "task_id": taskID, "stage": stage, "task_type": taskType,
				"model_name": modelName, "tokens_in": tokensIn, "tokens_out": tokensOut,
				"latency_ms": latencyMs, "status": status, "error_message": errorMessage,
				"created_at": createdAt.Format(time.RFC3339),
			})
		}
		addJSONToZip(zw, "llm_audit_log.json", llmLogs)
	}

	// 3. Healer health (best-effort)
	healerHealth := fetchHealerData("http://zovark-healer:8081/api/health")
	addJSONToZip(zw, "healer_health.json", healerHealth)

	// 4. Healer events (best-effort)
	healerEvents := fetchHealerData("http://zovark-healer:8081/api/events")
	addJSONToZip(zw, "healer_events.json", healerEvents)

	// 5. System info
	hostname, _ := os.Hostname()
	sysInfo := map[string]interface{}{
		"hostname":    hostname,
		"os":          runtime.GOOS,
		"arch":        runtime.GOARCH,
		"go_version":  runtime.Version(),
		"num_cpus":    runtime.NumCPU(),
		"goroutines":  runtime.NumGoroutine(),
		"uptime_secs": int(time.Since(startTime).Seconds()),
	}
	addJSONToZip(zw, "system_info.json", sysInfo)

	// 6. Task summary stats
	var totalTasks, completed, failed, pending int
	_ = dbPool.QueryRow(ctx, `
		SELECT COUNT(*),
		       COUNT(*) FILTER (WHERE status = 'completed'),
		       COUNT(*) FILTER (WHERE status = 'failed'),
		       COUNT(*) FILTER (WHERE status = 'pending')
		FROM agent_tasks WHERE tenant_id = $1
	`, tenantID).Scan(&totalTasks, &completed, &failed, &pending)

	taskSummary := map[string]interface{}{
		"total": totalTasks, "completed": completed, "failed": failed, "pending": pending,
	}
	addJSONToZip(zw, "task_summary.json", taskSummary)

	// 7. Manifest
	manifest := map[string]interface{}{
		"export_timestamp": time.Now().UTC().Format(time.RFC3339),
		"exporter_user":    userID,
		"tenant_id":        tenantID,
		"version":          "2.0",
		"signed":           false,
	}
	addJSONToZip(zw, "manifest.json", manifest)

	zw.Close()

	// Set response headers
	filename := fmt.Sprintf("zovark-diag-%s.zvk", time.Now().UTC().Format("20060102-150405"))
	c.Header("Content-Type", "application/zip")
	c.Header("Content-Disposition", fmt.Sprintf("attachment; filename=%s", filename))
	c.Data(http.StatusOK, "application/zip", buf.Bytes())
}

func addJSONToZip(zw *zip.Writer, filename string, data interface{}) {
	raw, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		raw = []byte(fmt.Sprintf(`{"error": "%s"}`, err.Error()))
	}
	// Scrub secrets from the JSON
	scrubbed := scrubSecrets(string(raw))

	w, err := zw.Create(filename)
	if err != nil {
		log.Printf("Failed to create zip entry %s: %v", filename, err)
		return
	}
	w.Write([]byte(scrubbed))
}

// probeDBHandler exercises the parameterised INSERT...RETURNING + DELETE round-trip
// that catches pgx<->PgBouncer prepared-statement collisions (SQLSTATE 08P01).
// Unlike GET /ready (which sends a parameterless SELECT 1 that pgx fast-paths
// regardless of mode), this handler hits the same code path that POST /api/v1/tasks
// uses to write to agent_tasks. If it returns 500 with 08P01 in the API log,
// the API is misconfigured against PgBouncer and the e2e probe Stage 0.5 will
// flag it as a DB-write failure rather than letting it surface as an "ingest stall".
//
// POST /api/v1/admin/diagnostics/probe-db
func probeDBHandler(c *gin.Context) {
	ctx := c.Request.Context()
	start := time.Now()

	tx, err := dbPool.Begin(ctx)
	if err != nil {
		respondInternalError(c, err, "probe-db")
		return
	}
	defer tx.Rollback(ctx)

	var rowID string
	if err := tx.QueryRow(ctx, "INSERT INTO probe_writes DEFAULT VALUES RETURNING id").Scan(&rowID); err != nil {
		respondInternalError(c, err, "probe-db")
		return
	}
	if _, err := tx.Exec(ctx, "DELETE FROM probe_writes WHERE id = $1", rowID); err != nil {
		respondInternalError(c, err, "probe-db")
		return
	}
	if err := tx.Commit(ctx); err != nil {
		respondInternalError(c, err, "probe-db")
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"ok":      true,
		"took_ms": time.Since(start).Milliseconds(),
		"row_id":  rowID,
	})
}

func fetchHealerData(url string) interface{} {
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return map[string]string{"error": fmt.Sprintf("healer unreachable: %s", err.Error())}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var result interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return map[string]string{"raw": string(body)}
	}
	return result
}
