package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

// oobBindAddr returns the bind address for the OOB watchdog.
// Audit 1.2: default to 127.0.0.1:9091 so a misconfigured host network doesn't
// expose DB/Redis/Temporal status to the LAN. The container compose file
// already port-maps to 127.0.0.1:9091, but defence in depth says we also bind
// loopback inside the container.
func oobBindAddr() string {
	if v := strings.TrimSpace(os.Getenv("ZOVARK_OOB_BIND")); v != "" {
		return v
	}
	return "127.0.0.1:9091"
}

// oobRequireToken returns the shared secret required in the X-Zovark-OOB-Token
// header. If empty, authentication is disabled (dev convenience — compose file
// runs on loopback anyway).
func oobRequireToken() string {
	return strings.TrimSpace(os.Getenv("ZOVARK_OOB_TOKEN"))
}

// oobAuthMiddleware enforces the shared-secret header when ZOVARK_OOB_TOKEN is
// set. Comparison is constant-time to eliminate timing oracles.
func oobAuthMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		required := oobRequireToken()
		if required == "" {
			next.ServeHTTP(w, r)
			return
		}
		provided := strings.TrimSpace(r.Header.Get("X-Zovark-OOB-Token"))
		if subtle.ConstantTimeCompare([]byte(provided), []byte(required)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// startOOBServer launches a plain net/http server on 127.0.0.1:9091 for
// out-of-band diagnostics. It is intentionally outside the Gin router so it
// stays reachable even when the main API is saturated or deadlocked.
//
// Audit 1.2: binds to loopback by default (env: ZOVARK_OOB_BIND override) and
// requires a shared-secret X-Zovark-OOB-Token header when ZOVARK_OOB_TOKEN is
// set. With loopback binding this endpoint is no longer reachable from the
// LAN even if the container's host network leaks.
func startOOBServer(ready chan<- struct{}) {
	mux := http.NewServeMux()
	mux.Handle("/debug/state", oobAuthMiddleware(http.HandlerFunc(oobStateHandler)))

	srv := &http.Server{
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
	}

	bindAddr := oobBindAddr()
	// Bind first, then signal ready — guarantees OOB is accepting connections
	// before main server starts.
	listener, err := net.Listen("tcp", bindAddr)
	if err != nil {
		log.Printf("[WARN] OOB server cannot bind %s: %v", bindAddr, err)
		close(ready)
		return
	}
	log.Printf("OOB watchdog listening on %s (auth=%v)", bindAddr, oobRequireToken() != "")
	close(ready)

	if err := srv.Serve(listener); err != nil && err != http.ErrServerClosed {
		log.Printf("[WARN] OOB server exited: %v", err)
	}
}

// oobStateHandler returns a comprehensive health snapshot as JSON.
// Each dependency check runs concurrently with a 3-second timeout and
// fails independently — a dead Redis does not block the Postgres result.
func oobStateHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	gpuTier := getEnvOrDefault("ZOVARK_GPU_TIER", "dev")
	temporalAddr := getEnvOrDefault("TEMPORAL_ADDRESS", "temporal:7233")
	endpointFast := getEnvOrDefault("ZOVARK_LLM_ENDPOINT_FAST",
		getEnvOrDefault("ZOVARK_LLM_ENDPOINT", "http://zovark-inference:8080/v1/chat/completions"))
	endpointCode := getEnvOrDefault("ZOVARK_LLM_ENDPOINT_CODE",
		getEnvOrDefault("ZOVARK_LLM_ENDPOINT", "http://zovark-inference:8080/v1/chat/completions"))

	var (
		wg      sync.WaitGroup
		mu      sync.Mutex
		results = make(map[string]string)
	)

	record := func(name, status string) {
		mu.Lock()
		results[name] = status
		mu.Unlock()
	}

	// --- Postgres check ---
	wg.Add(1)
	go func() {
		defer wg.Done()
		if dbPool == nil {
			record("postgres", "down")
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		start := time.Now()
		err := dbPool.Ping(ctx)
		elapsed := time.Since(start)
		if err != nil {
			record("postgres", "down")
		} else if elapsed > 2*time.Second {
			record("postgres", "degraded")
		} else {
			record("postgres", "ok")
		}
	}()

	// --- Redis check ---
	wg.Add(1)
	go func() {
		defer wg.Done()
		if redisClient == nil {
			record("redis", "down")
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		start := time.Now()
		err := redisClient.Ping(ctx).Err()
		elapsed := time.Since(start)
		if err != nil {
			record("redis", "down")
		} else if elapsed > 2*time.Second {
			record("redis", "degraded")
		} else {
			record("redis", "ok")
		}
	}()

	// --- Temporal check (TCP dial) ---
	wg.Add(1)
	go func() {
		defer wg.Done()
		start := time.Now()
		conn, err := net.DialTimeout("tcp", temporalAddr, 3*time.Second)
		elapsed := time.Since(start)
		if err != nil {
			record("temporal", "down")
			return
		}
		conn.Close()
		if elapsed > 2*time.Second {
			record("temporal", "degraded")
		} else {
			record("temporal", "ok")
		}
	}()

	// --- Inference checks ---
	// Derive health URL by replacing /v1/chat/completions with /health
	inferenceHealthURL := func(endpoint string) string {
		return strings.Replace(endpoint, "/v1/chat/completions", "/health", 1)
	}

	// Determine if fast and code share the same host:port
	sameHost := inferenceHostPort(endpointFast) == inferenceHostPort(endpointCode)

	checkInference := func(name, endpoint string) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			healthURL := inferenceHealthURL(endpoint)
			client := &http.Client{Timeout: 3 * time.Second}
			start := time.Now()
			resp, err := client.Get(healthURL)
			elapsed := time.Since(start)
			if err != nil {
				record(name, "down")
				return
			}
			resp.Body.Close()
			if resp.StatusCode >= 400 {
				record(name, "down")
			} else if elapsed > 2*time.Second {
				record(name, "degraded")
			} else {
				record(name, "ok")
			}
		}()
	}

	if sameHost {
		checkInference("inference", endpointFast)
	} else {
		checkInference("inference_fast", endpointFast)
		checkInference("inference_code", endpointCode)
	}

	wg.Wait()

	// --- Inference metrics (scrape /metrics from llama-server if available) ---
	inferenceMetrics := scrapeInferenceMetrics(endpointFast)

	// --- Runtime metrics ---
	var memStats runtime.MemStats
	runtime.ReadMemStats(&memStats)

	// --- Dedup stats (1-hour window from Redis counters) ---
	dedupStats := map[string]interface{}{}
	if redisClient != nil {
		dedupKeys := []string{"new_alert", "deduplicated", "severity_escalation", "retry_after_failure"}
		for _, k := range dedupKeys {
			val, err := redisClient.Get(context.Background(), "dedup:stats:"+k).Int64()
			if err == nil {
				dedupStats[k] = val
			} else {
				dedupStats[k] = 0
			}
		}
	}

	// --- Build response ---
	response := map[string]interface{}{
		"timestamp":       time.Now().UTC().Format(time.RFC3339),
		"api":             "ok",
		"postgres":        results["postgres"],
		"redis":           results["redis"],
		"temporal":        results["temporal"],
		"version":         "3.2.1",
		"uptime_seconds":  int(time.Since(startTime).Seconds()),
		"goroutines":      runtime.NumGoroutine(),
		"heap_mb":         memStats.HeapAlloc / 1024 / 1024,
		"gpu_tier":        gpuTier,
		"dedup_stats_1h":    dedupStats,
		"inference_metrics": inferenceMetrics,
	}

	// Add inference fields based on whether endpoints share the same host
	if sameHost {
		response["inference"] = results["inference"]
	} else {
		response["inference_fast"] = results["inference_fast"]
		response["inference_code"] = results["inference_code"]
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.Encode(response)
}

// scrapeInferenceMetrics fetches /metrics from llama-server and extracts key values.
// Returns empty map if metrics endpoint is unavailable (--metrics flag not enabled on llama-server).
func scrapeInferenceMetrics(endpoint string) map[string]interface{} {
	metricsURL := strings.Replace(endpoint, "/v1/chat/completions", "/metrics", 1)
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(metricsURL)
	if err != nil {
		return map[string]interface{}{"available": false}
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return map[string]interface{}{"available": false}
	}
	body := make([]byte, 64*1024) // 64KB max
	n, _ := resp.Body.Read(body)
	text := string(body[:n])

	result := map[string]interface{}{"available": true}
	// Parse key Prometheus metrics with simple line matching
	for _, line := range strings.Split(text, "\n") {
		if strings.HasPrefix(line, "#") || line == "" {
			continue
		}
		switch {
		case strings.HasPrefix(line, "llamacpp:kv_cache_usage_ratio "):
			result["kv_cache_usage_pct"] = parsePrometheusValue(line) * 100
		case strings.HasPrefix(line, "llamacpp:requests_processing "):
			result["requests_processing"] = int(parsePrometheusValue(line))
		case strings.HasPrefix(line, "llamacpp:requests_pending "):
			result["requests_pending"] = int(parsePrometheusValue(line))
		case strings.HasPrefix(line, "llamacpp:tokens_predicted_total "):
			result["tokens_generated_total"] = int(parsePrometheusValue(line))
		}
	}
	return result
}

func parsePrometheusValue(line string) float64 {
	parts := strings.Fields(line)
	if len(parts) >= 2 {
		var v float64
		fmt.Sscanf(parts[len(parts)-1], "%f", &v)
		return v
	}
	return 0
}

// inferenceHostPort extracts host:port from an LLM endpoint URL.
// Returns the raw string on parse failure so comparison still works.
func inferenceHostPort(endpoint string) string {
	u, err := url.Parse(endpoint)
	if err != nil {
		return endpoint
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return fmt.Sprintf("%s:%s", host, port)
}
