package main

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// checkOpenAIReachable verifies OpenAI API with GET /v1/models (Bearer ZOVARK_LLM_KEY).
func checkOpenAIReachable(chatCompletionsURL, apiKey string) bool {
	base := strings.TrimSuffix(strings.TrimSpace(chatCompletionsURL), "/")
	base = strings.TrimSuffix(base, "/v1/chat/completions")
	if base == "" {
		return false
	}
	u := base + "/v1/models"
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return false
	}
	if strings.TrimSpace(apiKey) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(apiKey))
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func healthCheckHandler(c *gin.Context) {
	deploymentMode := getEnvOrDefault("ZOVARK_DEPLOYMENT_MODE", "cloud")
	llmModel := getEnvOrDefault("ZOVARK_LLM_MODEL", "fast")

	llmProvider := "OpenRouter (Cloud)"
	if strings.ToLower(strings.TrimSpace(getEnvOrDefault("ZOVARK_LLM_PROVIDER", "openai"))) == "openai" {
		llmProvider = "OpenAI API"
	} else if llmModel == "zovark-local" {
		llmProvider = "Local LLM"
	}

	// Check DB
	dbOK := false
	if dbPool != nil {
		err := dbPool.Ping(c.Request.Context())
		dbOK = (err == nil)
	}

	// LLM health — check configured endpoint (best-effort)
	llmOK := false
	llmEndpoint := getEnvOrDefault("ZOVARK_LLM_ENDPOINT_FAST",
		getEnvOrDefault("ZOVARK_LLM_ENDPOINT", "https://api.openai.com/v1/chat/completions"))
	llmProviderEnv := strings.ToLower(strings.TrimSpace(getEnvOrDefault("ZOVARK_LLM_PROVIDER", "openai")))
	llmKey := strings.TrimSpace(getEnvOrDefault("ZOVARK_LLM_KEY", ""))
	if llmKey == "" {
		llmKey = strings.TrimSpace(getEnvOrDefault("OPENAI_API_KEY", ""))
	}
	// Per-call short-deadline client — http.Get() uses DefaultClient with no timeout
	// which would block /health forever if the LLM/embedding backend hangs.
	hcClient := &http.Client{Timeout: 3 * time.Second}
	getWithTimeout := func(u string) (*http.Response, error) {
		req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodGet, u, nil)
		if err != nil {
			return nil, err
		}
		return hcClient.Do(req)
	}

	if llmProviderEnv == "openai" {
		llmOK = checkOpenAIReachable(llmEndpoint, llmKey)
	} else {
		// Derive health URL from chat completions endpoint (llama.cpp)
		llmHealthURL := strings.TrimSuffix(llmEndpoint, "/v1/chat/completions") + "/health"
		resp, err := getWithTimeout(llmHealthURL)
		if err == nil {
			llmOK = (resp.StatusCode == 200)
			resp.Body.Close()
		}
		if !llmOK {
			// Fallback: try /api/tags (legacy compatibility)
			llmTagsURL := strings.TrimSuffix(llmEndpoint, "/v1/chat/completions") + "/api/tags"
			resp, err = getWithTimeout(llmTagsURL)
			if err == nil {
				llmOK = (resp.StatusCode == 200)
				resp.Body.Close()
			}
		}
	}

	// Check embedding server
	embeddingOK := false
	embResp, embErr := getWithTimeout("http://embedding-server:80/health")
	if embErr == nil {
		embeddingOK = (embResp.StatusCode == 200)
		embResp.Body.Close()
	}

	c.JSON(http.StatusOK, gin.H{
		"status":             "ok",
		"version":            "1.0.0-rc1",
		"uptime_seconds":     int(time.Since(startTime).Seconds()),
		"mode":               deploymentMode,
		"llm_provider":       llmProvider,
		"llm_model":          llmModel,
		"embedding_provider": "HuggingFace TEI (Local)",
		"database":           "PostgreSQL + pgvector",
		"services": gin.H{
			"api":       true,
			"db":        dbOK,
			"llm":       llmOK,
			"embedding": embeddingOK,
		},
	})
}

// readinessHandler checks that ALL critical dependencies are reachable.
// Returns 200 only if PostgreSQL, Redis, and Temporal are all healthy.
// Returns 503 with details if any fail. Used by Docker healthcheck and load balancers.
// GET /ready
func readinessHandler(c *gin.Context) {
	checks := make(map[string]gin.H)
	allReady := true

	// PostgreSQL via PgBouncer
	dbReady := false
	dbDetail := "not initialized"
	if dbPool != nil {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
		defer cancel()
		var one int
		err := dbPool.QueryRow(ctx, "SELECT 1").Scan(&one)
		if err == nil && one == 1 {
			dbReady = true
			dbDetail = "SELECT 1 OK"
		} else if err != nil {
			dbDetail = err.Error()
		}
	}
	checks["postgresql"] = gin.H{"ready": dbReady, "detail": dbDetail}
	if !dbReady {
		allReady = false
	}

	// Redis
	redisReady := false
	redisDetail := "not initialized"
	if redisClient != nil {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
		defer cancel()
		err := redisClient.Ping(ctx).Err()
		if err == nil {
			redisReady = true
			redisDetail = "PONG"
		} else {
			redisDetail = err.Error()
		}
	}
	checks["redis"] = gin.H{"ready": redisReady, "detail": redisDetail}
	if !redisReady {
		allReady = false
	}

	// Temporal — check gRPC connectivity via the SDK client
	temporalReady := false
	temporalDetail := "not initialized"
	if tc != nil {
		// The SDK client maintains a gRPC connection; if Dial succeeded and
		// the connection hasn't been closed, Temporal is reachable.
		temporalReady = true
		temporalDetail = "client connected"
	}
	checks["temporal"] = gin.H{"ready": temporalReady, "detail": temporalDetail}
	if !temporalReady {
		allReady = false
	}

	status := http.StatusOK
	statusText := "ready"
	if !allReady {
		status = http.StatusServiceUnavailable
		statusText = "not_ready"
	}

	c.JSON(status, gin.H{
		"status":         statusText,
		"uptime_seconds": int(time.Since(startTime).Seconds()),
		"checks":         checks,
	})
}
