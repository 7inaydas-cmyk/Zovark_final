package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"time"
)

// ============================================================
// LAYER 1: PRE-TEMPORAL REDIS DEDUP (v2 — investigation-aware)
//
// Mirrors the Python dedup logic in worker/stages/ingest.py:166-188.
// Prevents duplicate alerts from creating redundant Temporal workflows.
//
// v2 changes (from blind TTL):
//   - Stores structured JSON entries (task_id, status, severity, verdict)
//   - Allows retry when previous investigation failed/errored
//   - Allows escalation when new alert has higher severity
//   - Worker updates entry on completion (via store.py)
//
// Fail-open: if Redis is unavailable, skip dedup and proceed normally.
// ============================================================

var apiDedupEnabled = true

func init() {
	if v := os.Getenv("ZOVARK_API_DEDUP_ENABLED"); v == "false" {
		apiDedupEnabled = false
	}
}

// Timestamp patterns to normalize (must match Python exactly)
var timestampPatterns = []*regexp.Regexp{
	regexp.MustCompile(`\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?`),
	regexp.MustCompile(`\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}`),
	regexp.MustCompile(`\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}`),
}

// Severity-based TTLs (in seconds) — must match Python DEDUP_TTL
var dedupTTL = map[string]int{
	"critical": 900,  // 15 min — active incidents produce sustained identical alerts
	"high":     600,  // 10 min
	"medium":   300,  // 5 min
	"low":      3600, // 1 hour
	"info":     7200, // 2 hours
}

// DedupEntry is the structured value stored in Redis for v2 dedup.
type DedupEntry struct {
	TaskID    string `json:"task_id"`
	Status    string `json:"status"`    // pending | completed | failed | error
	Verdict   string `json:"verdict"`   // empty if not yet completed
	RiskScore int    `json:"risk_score"`
	Severity  string `json:"severity"`  // severity of original alert
	CreatedAt string `json:"created_at"`
}

// normalizeRawLog strips timestamps from raw_log for dedup hashing.
// Matches Python _normalize_raw_log() exactly.
func normalizeRawLog(raw string) string {
	for _, p := range timestampPatterns {
		raw = p.ReplaceAllString(raw, "TIMESTAMP")
	}
	return raw
}

// ocsfDedupCanon is JSON-marshaled for deterministic pre-Temporal dedup (sorted field order via struct tags).
type ocsfDedupCanon struct {
	ActorUserName string `json:"actor.user.name"`
	ClassUID      string `json:"class_uid"`
	DstEndpointIP string `json:"dst_endpoint.ip"`
	RuleName      string `json:"rule_name"`
	SrcEndpointIP string `json:"src_endpoint.ip"`
}

func isOCSFEvent(m map[string]interface{}) bool {
	if m == nil {
		return false
	}
	_, ok := m["class_uid"]
	return ok
}

func ocsfClassUIDString(m map[string]interface{}) string {
	v, ok := m["class_uid"]
	if !ok {
		return ""
	}
	switch t := v.(type) {
	case float64:
		return fmt.Sprintf("%.0f", t)
	case json.Number:
		return t.String()
	case int:
		return fmt.Sprintf("%d", t)
	case int64:
		return fmt.Sprintf("%d", t)
	default:
		return strings.TrimSpace(fmt.Sprintf("%v", v))
	}
}

func ocsfActorUserName(m map[string]interface{}) string {
	a, ok := m["actor"].(map[string]interface{})
	if !ok {
		return ""
	}
	u, ok := a["user"].(map[string]interface{})
	if !ok {
		return ""
	}
	if s, ok := u["name"].(string); ok {
		return s
	}
	return ""
}

func ocsfRuleNameForDedup(m map[string]interface{}) string {
	if fi, ok := m["finding_info"].(map[string]interface{}); ok {
		if t, ok := fi["title"].(string); ok && t != "" {
			return t
		}
	}
	return getStringField(m, "rule_name")
}

func computeOCSFCanonicalDedupHash(ocsf map[string]interface{}) string {
	canon := ocsfDedupCanon{
		SrcEndpointIP: endpointIPStr(ocsf, "src_endpoint"),
		DstEndpointIP: endpointIPStr(ocsf, "dst_endpoint"),
		RuleName:      ocsfRuleNameForDedup(ocsf),
		ActorUserName: ocsfActorUserName(ocsf),
		ClassUID:      ocsfClassUIDString(ocsf),
	}
	data, _ := json.Marshal(canon)
	hash := sha256.Sum256(data)
	return fmt.Sprintf("%x", hash)
}

func endpointIPStr(m map[string]interface{}, key string) string {
	if m == nil {
		return ""
	}
	ep, ok := m[key].(map[string]interface{})
	if !ok {
		return ""
	}
	if s, ok := ep["ip"].(string); ok {
		return s
	}
	return ""
}

// computeAlertHash is the legacy pre-OCSF canonical hash (ZCS-style flat siem_event).
func computeAlertHash(input map[string]interface{}) string {
	siem := input
	if se, ok := input["siem_event"].(map[string]interface{}); ok {
		siem = se
	}

	canonical := map[string]string{
		"destination_ip": getStringField(siem, "destination_ip", "dest_ip"),
		"hostname":       getStringField(siem, "hostname"),
		"raw_log":        normalizeRawLog(getStringField(siem, "raw_log")),
		"rule_name":      getStringField(siem, "rule_name"),
		"source_ip":      getStringField(siem, "source_ip"),
		"username":       getStringField(siem, "username"),
	}

	data, _ := json.Marshal(canonical)
	hash := sha256.Sum256(data)
	return fmt.Sprintf("%x", hash)
}

// computeDedupHash uses OCSF canonical fields when siem_event carries class_uid; otherwise legacy hash.
func computeDedupHash(input map[string]interface{}) string {
	if se, ok := input["siem_event"].(map[string]interface{}); ok && isOCSFEvent(se) {
		return computeOCSFCanonicalDedupHash(se)
	}
	return computeAlertHash(input)
}

// getStringField extracts a string from a map, trying multiple key names.
func getStringField(m map[string]interface{}, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k].(string); ok {
			return v
		}
	}
	return ""
}

// severityRank returns a numeric rank for severity comparison.
func severityRank(s string) int {
	switch strings.ToLower(s) {
	case "critical":
		return 5
	case "high":
		return 4
	case "medium":
		return 3
	case "low":
		return 2
	case "info":
		return 1
	default:
		return 0
	}
}

// checkPreDedup checks if an identical alert was already submitted recently.
// Returns (isDuplicate, existingTaskID, reason).
// Fail-open: returns (false, "", "redis_error") if Redis is unavailable.
func checkPreDedup(ctx context.Context, taskType string, input map[string]interface{}) (bool, string) {
	if !apiDedupEnabled || redisClient == nil {
		return false, ""
	}

	hash := computeDedupHash(input)
	key := "dedup:exact:" + hash

	val, err := redisClient.Get(ctx, key).Result()
	if err != nil {
		// Key not found or Redis error — not a duplicate
		return false, ""
	}

	// Try to parse as v2 JSON entry
	var entry DedupEntry
	if err := json.Unmarshal([]byte(val), &entry); err != nil {
		// Old v1 plain-string entry — treat as duplicate with existing task_id
		slog.InfoContext(ctx, "dedup_pre_temporal_hit",
			slog.String("dedup.version", "v1"),
			slog.String("dedup.hash_prefix", hash[:16]),
			slog.String("dedup.existing_task_id", val),
			slog.String("outcome", "duplicate"),
		)
		return true, val
	}

	// v2 structured entry — apply investigation-aware logic
	newSeverity := getInputSeverity(input)

	// Case 1: Previous investigation FAILED — allow retry
	if entry.Status == "error" || entry.Status == "failed" {
		slog.InfoContext(ctx, "dedup_retry_after_failure",
			slog.String("dedup.hash_prefix", hash[:16]),
			slog.String("dedup.old_task_id", entry.TaskID),
			slog.String("dedup.old_status", entry.Status),
			slog.String("outcome", "retry"),
		)
		// Delete the stale entry; if Del fails the retry path may keep bouncing — log and escalate.
		if delErr := redisClient.Del(ctx, key).Err(); delErr != nil {
			slog.ErrorContext(ctx, "dedup_del_failed",
				slog.String("dedup.hash_prefix", hash[:16]),
				slog.String("err", delErr.Error()),
			)
		}
		recordDedupDecision(ctx, "retry_after_failure")
		return false, ""
	}

	// Case 2: New alert is HIGHER severity — escalation bypass
	if severityRank(newSeverity) > severityRank(entry.Severity) {
		slog.InfoContext(ctx, "dedup_severity_escalation",
			slog.String("dedup.hash_prefix", hash[:16]),
			slog.String("dedup.old_severity", entry.Severity),
			slog.String("dedup.new_severity", newSeverity),
			slog.String("dedup.old_task_id", entry.TaskID),
			slog.String("outcome", "escalation_bypass"),
		)
		recordDedupDecision(ctx, "severity_escalation")
		return false, ""
	}

	// Case 3: Normal duplicate — dedup
	slog.InfoContext(ctx, "dedup_pre_temporal_hit",
		slog.String("dedup.version", "v2"),
		slog.String("dedup.hash_prefix", hash[:16]),
		slog.String("dedup.existing_task_id", entry.TaskID),
		slog.String("dedup.existing_status", entry.Status),
		slog.String("outcome", "duplicate"),
	)
	recordDedupDecision(ctx, "deduplicated")
	return true, entry.TaskID
}

// registerPreDedup registers an alert hash in Redis after the workflow is created.
// Uses v2 structured JSON entry.
func registerPreDedup(ctx context.Context, taskType string, input map[string]interface{}, taskID, severity string) {
	if !apiDedupEnabled || redisClient == nil {
		return
	}

	hash := computeDedupHash(input)
	key := "dedup:exact:" + hash

	entry := DedupEntry{
		TaskID:    taskID,
		Status:    "pending",
		Severity:  strings.ToLower(severity),
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	data, _ := json.Marshal(entry)

	sev := strings.ToLower(severity)
	ttl, ok := dedupTTL[sev]
	if !ok {
		ttl = 300
	}

	err := redisClient.SetEx(ctx, key, string(data), time.Duration(ttl)*time.Second).Err()
	if err != nil {
		slog.ErrorContext(ctx, "dedup_register_failed",
			slog.String("outcome", "error"),
			slog.String("dedup.hash_prefix", hash[:16]),
			slog.Any("error", err),
		)
	}

	recordDedupDecision(ctx, "new_alert")
}

// getInputSeverity extracts severity from the task input.
func getInputSeverity(input map[string]interface{}) string {
	if s, ok := input["severity"].(string); ok {
		return strings.ToLower(s)
	}
	return "medium"
}

// clearDedupEntry removes a dedup entry (used by force_reinvestigate).
func clearDedupEntry(ctx context.Context, input map[string]interface{}) {
	if redisClient == nil {
		return
	}
	hash := computeDedupHash(input)
	key := "dedup:exact:" + hash
	redisClient.Del(ctx, key)
}

// recordDedupDecision increments a Redis counter for observability.
func recordDedupDecision(ctx context.Context, decision string) {
	if redisClient == nil {
		return
	}
	key := "dedup:stats:" + decision
	redisClient.Incr(ctx, key)
	redisClient.Expire(ctx, key, 1*time.Hour)
}
