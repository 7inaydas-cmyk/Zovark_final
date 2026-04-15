package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// ============================================================
// SIEM VERDICT PUSH-BACK
//
// After an investigation completes, POST the verdict back to the
// originating SIEM (Splunk HEC, Elastic, or generic webhook).
//
// Configuration via system_configs table:
//   siem.pushback.enabled   = true/false
//   siem.pushback.type      = splunk | elastic | webhook
//   siem.pushback.url       = https://splunk:8088/services/collector/event
//   siem.pushback.token     = <HEC token or Bearer token> (is_secret=true)
//   siem.pushback.index     = main (Splunk-specific)
//   siem.pushback.verify_tls = true/false
//
// Fire-and-forget: push-back failure never blocks the pipeline.
// ============================================================

type pushbackConfig struct {
	Enabled   bool
	Type      string // splunk, elastic, webhook
	URL       string
	Token     string
	Index     string
	VerifyTLS bool
}

type investigationResult struct {
	TaskID     string                 `json:"task_id"`
	TenantID   string                 `json:"tenant_id"`
	TaskType   string                 `json:"task_type"`
	Verdict    string                 `json:"verdict"`
	RiskScore  int                    `json:"risk_score"`
	Summary    string                 `json:"summary"`
	MITRE      []interface{}          `json:"mitre_techniques"`
	IOCs       []interface{}          `json:"iocs"`
	Severity   string                 `json:"severity"`
	Output     map[string]interface{} `json:"output"`
}

func getPushbackConfig(ctx context.Context) pushbackConfig {
	cfg := pushbackConfig{VerifyTLS: true}
	if dbPool == nil {
		return cfg
	}

	rows, err := dbPool.Query(ctx,
		`SELECT config_key, config_value FROM system_configs WHERE config_key LIKE 'siem.pushback.%'`)
	if err != nil {
		return cfg
	}
	defer rows.Close()

	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			continue
		}
		switch key {
		case "siem.pushback.enabled":
			cfg.Enabled = value == "true"
		case "siem.pushback.type":
			cfg.Type = value
		case "siem.pushback.url":
			cfg.URL = value
		case "siem.pushback.token":
			cfg.Token = value
		case "siem.pushback.index":
			cfg.Index = value
		case "siem.pushback.verify_tls":
			cfg.VerifyTLS = value != "false"
		}
	}
	return cfg
}

func buildSplunkPayload(inv investigationResult, cfg pushbackConfig) ([]byte, error) {
	index := cfg.Index
	if index == "" {
		index = "main"
	}

	payload := map[string]interface{}{
		"event": map[string]interface{}{
			"zovark_investigation_id": inv.TaskID,
			"zovark_verdict":          inv.Verdict,
			"zovark_risk_score":       inv.RiskScore,
			"zovark_summary":          inv.Summary,
			"zovark_mitre_techniques": inv.MITRE,
			"zovark_severity":         inv.Severity,
			"zovark_task_type":        inv.TaskType,
			"zovark_iocs":             inv.IOCs,
		},
		"sourcetype": "zovark:verdict",
		"source":     "zovark",
		"index":      index,
	}
	return json.Marshal(payload)
}

func buildElasticPayload(inv investigationResult) ([]byte, error) {
	payload := map[string]interface{}{
		"@timestamp":              time.Now().UTC().Format(time.RFC3339),
		"zovark.investigation_id": inv.TaskID,
		"zovark.verdict":          inv.Verdict,
		"zovark.risk_score":       inv.RiskScore,
		"zovark.summary":          inv.Summary,
		"zovark.mitre_techniques": inv.MITRE,
		"zovark.severity":         inv.Severity,
		"zovark.task_type":        inv.TaskType,
		"event.kind":              "enrichment",
		"event.category":          []string{"intrusion_detection"},
		"event.outcome":           "success",
	}
	return json.Marshal(payload)
}

func buildWebhookPayload(inv investigationResult) ([]byte, error) {
	payload := map[string]interface{}{
		"event_type":       "verdict",
		"investigation_id": inv.TaskID,
		"tenant_id":        inv.TenantID,
		"task_type":        inv.TaskType,
		"verdict":          inv.Verdict,
		"risk_score":       inv.RiskScore,
		"severity":         inv.Severity,
		"summary":          inv.Summary,
		"mitre_techniques": inv.MITRE,
		"iocs":             inv.IOCs,
		"timestamp":        time.Now().UTC().Format(time.RFC3339),
	}
	return json.Marshal(payload)
}

// isPrivateOrLinkLocal blocks SSRF to RFC1918, loopback, link-local, and cloud metadata endpoints.
// Returns (blocked, reason).
func isPrivateOrLinkLocal(rawURL string) (bool, string) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return true, "malformed url"
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return true, "non-http(s) scheme"
	}
	host := u.Hostname()
	if host == "" {
		return true, "empty host"
	}
	// Block well-known metadata hostnames.
	lower := strings.ToLower(host)
	if lower == "metadata.google.internal" || lower == "metadata" || lower == "localhost" {
		return true, "blocked hostname: " + lower
	}
	// Resolve to IPs and check each.
	ips, err := net.LookupIP(host)
	if err != nil || len(ips) == 0 {
		// If we cannot resolve, err on the side of caution.
		return true, "resolve failed"
	}
	for _, ip := range ips {
		if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsPrivate() || ip.IsUnspecified() {
			return true, "private/loopback/link-local ip: " + ip.String()
		}
		// Cloud metadata IPv4 (169.254.169.254 is caught by IsLinkLocalUnicast; also catch 100.100.100.200 Alibaba etc.).
		if ip.String() == "100.100.100.200" {
			return true, "cloud metadata ip"
		}
	}
	return false, ""
}

// pushVerdictToSIEM sends the investigation verdict to the configured SIEM.
// Fire-and-forget — errors are logged but never propagated.
func pushVerdictToSIEM(ctx context.Context, inv investigationResult) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[PUSHBACK] panic recovered in pushVerdictToSIEM for task %s: %v", inv.TaskID, r)
		}
	}()

	cfg := getPushbackConfig(ctx)
	if !cfg.Enabled || cfg.URL == "" {
		return
	}

	// SSRF deny-list — refuse to POST to private/link-local/metadata addresses.
	if blocked, reason := isPrivateOrLinkLocal(cfg.URL); blocked {
		log.Printf("[PUSHBACK] refused: %s (%s)", cfg.URL, reason)
		return
	}

	var payload []byte
	var err error
	headers := map[string]string{"Content-Type": "application/json"}

	switch cfg.Type {
	case "splunk":
		payload, err = buildSplunkPayload(inv, cfg)
		if cfg.Token != "" {
			headers["Authorization"] = "Splunk " + cfg.Token
		}
	case "elastic":
		payload, err = buildElasticPayload(inv)
		if cfg.Token != "" {
			headers["Authorization"] = "Bearer " + cfg.Token
		}
	case "webhook":
		payload, err = buildWebhookPayload(inv)
		if cfg.Token != "" {
			headers["Authorization"] = "Bearer " + cfg.Token
		}
		headers["X-Zovark-Event"] = "verdict"
	default:
		log.Printf("[PUSHBACK] Unknown type: %s", cfg.Type)
		return
	}

	if err != nil {
		log.Printf("[PUSHBACK] Failed to build payload: %v", err)
		return
	}

	url := cfg.URL
	if cfg.Type == "elastic" {
		url = cfg.URL + "/zovark-verdicts/_doc"
	}

	// In production, refuse to disable TLS verification regardless of operator config.
	// The VerifyTLS knob is honoured only when ZOVARK_DEPLOYMENT_MODE != "prod".
	verifyTLS := cfg.VerifyTLS
	if strings.EqualFold(strings.TrimSpace(os.Getenv("ZOVARK_DEPLOYMENT_MODE")), "prod") && !verifyTLS {
		log.Printf("[PUSHBACK] refusing to honour verify_tls=false in production mode; forcing verification on")
		verifyTLS = true
	}

	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{
				InsecureSkipVerify: !verifyTLS,
				MinVersion:         tls.VersionTLS12,
			},
		},
	}

	// 2 attempts with 10s timeout
	for attempt := 0; attempt < 2; attempt++ {
		req, reqErr := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(payload))
		if reqErr != nil {
			log.Printf("[PUSHBACK] attempt %d: NewRequest failed: %v", attempt+1, reqErr)
			return
		}
		for k, v := range headers {
			req.Header.Set(k, v)
		}

		resp, err := client.Do(req)
		if err != nil {
			log.Printf("[PUSHBACK] Attempt %d failed: %v", attempt+1, err)
			continue
		}
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			log.Printf("[PUSHBACK] Verdict pushed to %s for task %s (verdict=%s)", cfg.Type, inv.TaskID, inv.Verdict)
			return
		}
		log.Printf("[PUSHBACK] Attempt %d: HTTP %d", attempt+1, resp.StatusCode)
	}

	log.Printf("[PUSHBACK] Failed after 2 attempts for task %s", inv.TaskID)
}

// triggerPushbackFromNotify is called when a task_completed notification arrives.
// Loads the investigation and fires push-back in a goroutine.
func triggerPushbackFromNotify(taskID, tenantID string) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("[PUSHBACK] panic recovered in triggerPushbackFromNotify for task %s: %v", taskID, r)
			}
		}()
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()

		if dbPool == nil {
			return
		}

		cfg := getPushbackConfig(ctx)
		if !cfg.Enabled {
			return
		}

		// Load investigation from DB
		var inv investigationResult
		inv.TaskID = taskID
		inv.TenantID = tenantID

		row := dbPool.QueryRow(ctx,
			`SELECT task_type, COALESCE(output->>'verdict', ''), COALESCE((output->>'risk_score')::int, 0),
			        COALESCE(output->>'plain_english_summary', ''), COALESCE(severity, 'medium')
			 FROM agent_tasks WHERE id = $1 AND tenant_id = $2`,
			taskID, tenantID)

		if err := row.Scan(&inv.TaskType, &inv.Verdict, &inv.RiskScore, &inv.Summary, &inv.Severity); err != nil {
			log.Printf("[PUSHBACK] Failed to load task %s: %v", taskID, err)
			return
		}

		if inv.Verdict == "" || inv.Verdict == "pending" {
			return
		}

		pushVerdictToSIEM(ctx, inv)
	}()
}
