package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

// ============================================================
// WEBHOOK RECEIVER
// ============================================================

func webhookAlertHandler(c *gin.Context) {
	sourceID := c.Param("source_id")

	// 1. Look up the log_source
	var tenantID, sourceType, sourceName string
	var connConfig map[string]interface{}
	var isActive bool

	err := dbPool.QueryRow(c.Request.Context(),
		"SELECT tenant_id, source_type, name, connection_config, is_active FROM log_sources WHERE id = $1",
		sourceID,
	).Scan(&tenantID, &sourceType, &sourceName, &connConfig, &isActive)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "unknown source"})
		return
	}
	if !isActive {
		c.JSON(http.StatusForbidden, gin.H{"error": "source is deactivated"})
		return
	}

	// 2. Read body (limit to 1MB to prevent OOM DoS)
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request body too large (max 1MB)"})
		return
	}

	// 3. HMAC-SHA256 validation — FIX #4: require webhook_secret; reject unsigned calls
	secret, hasSecret := connConfig["webhook_secret"].(string)
	if !hasSecret || secret == "" {
		c.JSON(http.StatusForbidden, gin.H{"error": "log source has no webhook_secret configured — unsigned webhooks are not accepted"})
		return
	}
	sig := c.GetHeader("X-Webhook-Signature")
	if sig == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing X-Webhook-Signature header"})
		return
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	expected := hex.EncodeToString(mac.Sum(nil))
	// Constant-time comparison — prevents timing side-channel on signature forgery.
	if !hmac.Equal([]byte(sig), []byte(expected)) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid webhook signature"})
		return
	}

	// 4. Parse payload
	var payload map[string]interface{}
	if err := json.Unmarshal(body, &payload); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON payload"})
		return
	}

	// 5. OCSF boundary normalization (Sentinel vs generic webhook vs other shapes)
	ocsf := ChooseSentinelOrGeneric(payload)
	alertName, severity, sourceIP, destIP, ruleName := OCSFSIEMColumnValues(ocsf)

	// TODO(security): Go-side sanitization is limited to control-char stripping and
	// field truncation (see sanitizeSIEMField / autoInvestigateAlert). Deep prompt-injection
	// neutralization, IoC extraction, and integrity hashing are handled on the Python side
	// by worker/security/alert_sanitizer.py (AlertSanitizer) before any embedding or LLM call.
	// If a Go-native sanitization layer is needed in future, mirror the 5-stage pipeline
	// defined in that module.

	// 6. Insert into siem_alerts
	alertID := uuid.New().String()
	autoInvestigate := false
	if ai, ok := connConfig["auto_investigate"].(bool); ok {
		autoInvestigate = ai
	}

	rawCopy := append([]byte(nil), body...)
	normJSON, _ := json.Marshal(ocsf)

	_, err = dbPool.Exec(c.Request.Context(),
		`INSERT INTO siem_alerts (id, tenant_id, log_source_id, alert_name, severity, source_ip, dest_ip, rule_name, raw_event, normalized_event, status, auto_investigate)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'new', $11)`,
		alertID, tenantID, sourceID,
		alertName, severity, sourceIP, destIP, ruleName,
		rawCopy, normJSON,
		autoInvestigate,
	)
	if err != nil {
		respondInternalError(c, err, "store SIEM alert")
		return
	}

	// 7. Update log_source: last_event_at, event_count++
	_, _ = dbPool.Exec(c.Request.Context(),
		"UPDATE log_sources SET last_event_at = NOW(), event_count = event_count + 1, updated_at = NOW() WHERE id = $1",
		sourceID,
	)

	// 8. Auto-investigate if configured
	var investigationID *string
	if autoInvestigate {
		taskID, err := autoInvestigateAlert(c.Request.Context(), tenantID, alertID, ocsf, rawCopy)
		if err != nil {
			log.Printf("Auto-investigate failed for alert %s: %v", alertID, err)
		} else {
			investigationID = &taskID
		}
	}

	c.JSON(http.StatusOK, gin.H{
		"alert_id":         alertID,
		"investigation_id": investigationID,
		"status":           "received",
	})
}

// ocsfFromStoredNormalized returns OCSF if stored normalized_event already has class_uid, else maps flat ZCS to OCSF.
func ocsfFromStoredNormalized(stored map[string]interface{}) map[string]interface{} {
	if stored == nil {
		return NormalizeFlatSIEMToOCSF(map[string]interface{}{})
	}
	if _, ok := stored["class_uid"]; ok {
		return stored
	}
	return NormalizeFlatSIEMToOCSF(stored)
}

// sanitizeSIEMField strips control characters and truncates SIEM field values
// to prevent prompt injection via crafted alert data (Security P0#10).
func sanitizeSIEMField(value string, maxLen int) string {
	// Strip control characters and newlines
	cleaned := strings.Map(func(r rune) rune {
		if r < 32 || r == 127 {
			return ' '
		}
		return r
	}, value)
	// Truncate to max length
	if len(cleaned) > maxLen {
		cleaned = cleaned[:maxLen]
	}
	return strings.TrimSpace(cleaned)
}

// sanitizeSIEMMap recursively sanitizes all string values in a map.
// FIX #5: prevents unsanitized siem_event fields from reaching LLM prompt construction.
func sanitizeSIEMMap(m map[string]interface{}) map[string]interface{} {
	out := make(map[string]interface{}, len(m))
	for k, v := range m {
		switch val := v.(type) {
		case string:
			out[k] = sanitizeSIEMField(val, 2000)
		case map[string]interface{}:
			out[k] = sanitizeSIEMMap(val)
		default:
			out[k] = v
		}
	}
	return out
}

func autoInvestigateAlert(ctx context.Context, tenantID, alertID string, ocsf map[string]interface{}, rawInputJSON []byte) (string, error) {
	// Map severity to task type
	taskType := "log_analysis"
	if sev, ok := ocsf["severity"].(string); ok {
		switch sev {
		case "critical", "high":
			taskType = "incident_response"
		case "medium":
			taskType = "threat_hunt"
		}
	}

	an, sevStr, sip, dip, rn := OCSFSIEMColumnValues(ocsf)
	alertName := sanitizeSIEMField(an, 200)
	sourceIP := sanitizeSIEMField(sip, 45)
	destIP := sanitizeSIEMField(dip, 45)
	ruleName := sanitizeSIEMField(rn, 200)
	severity := sanitizeSIEMField(sevStr, 20)

	prompt := fmt.Sprintf(
		"Investigate SIEM alert: %s. Source: %s, Dest: %s. Rule: %s. Severity: %s.",
		alertName, sourceIP, destIP, ruleName, severity,
	)

	taskID := uuid.New().String()
	traceID := zovarkTraceUUIDFromContext(ctx)

	rawIn := append([]byte(nil), rawInputJSON...)
	if len(rawIn) == 0 {
		rawIn, _ = json.Marshal(ocsf)
	}
	input := map[string]interface{}{
		"prompt":        prompt,
		"siem_alert_id": alertID,
		"siem_event":    ocsf,
		"trace_id":      traceID,
		"ingest_source": "siem_webhook",
		"severity":      severity,
		"source_ip":     sourceIP,
		"dest_ip":       destIP,
		"rule_name":     ruleName,
	}
	dedupHash := computeDedupHash(input)

	// Insert task inside explicit transaction
	// CRITICAL: tx.Commit() MUST happen BEFORE ExecuteWorkflow() to avoid race condition
	dbCtx, dbCancel := dbContextWithTimeout(ctx)
	tx, err := dbPool.Begin(dbCtx)
	if err != nil {
		dbCancel()
		return "", fmt.Errorf("failed to begin transaction: %w", err)
	}
	defer tx.Rollback(dbCtx) // no-op after commit

	_, err = tx.Exec(dbCtx,
		`INSERT INTO agent_tasks (id, tenant_id, task_type, input, raw_input, dedup_hash, status, created_at, trace_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
		taskID, tenantID, taskType, input, rawIn, dedupHash, "pending", time.Now(), traceID,
	)
	if err != nil {
		dbCancel()
		if isLockTimeout(err) {
			HandlePostgresLock(nil, tenantID, taskID, severity, "INSERT", "agent_tasks", 5000)
		}
		return "", fmt.Errorf("failed to create task: %w", err)
	}

	// Audit log (inside same transaction)
	auditDetails, _ := json.Marshal(map[string]string{"task_id": taskID, "alert_name": alertName})
	_, _ = tx.Exec(dbCtx,
		"INSERT INTO agent_audit_log (tenant_id, action, resource_type, resource_id, details) VALUES ($1, $2, $3, $4, $5)",
		tenantID, "task_created", "task", taskID,
		string(auditDetails),
	)

	// COMMIT — data now visible to all connections (including worker's fetch_task)
	if err := tx.Commit(dbCtx); err != nil {
		dbCancel()
		return "", fmt.Errorf("failed to commit task transaction: %w", err)
	}
	dbCancel()

	pubCtx, pubCancel := context.WithTimeout(ctx, 15*time.Second)
	defer pubCancel()
	if err := publishTaskNew(pubCtx, tenantID, taskID, taskType, input); err != nil {
		cleanCtx, cleanCancel := detachedCleanupCtx()
		_, _ = dbPool.Exec(cleanCtx, "UPDATE agent_tasks SET status = 'failed' WHERE id = $1", taskID)
		cleanCancel()
		return "", fmt.Errorf("failed to publish task to redpanda: %w", err)
	}
	recordWorkflowStart(ctx, "task-"+taskID)

	// Link the alert to the investigation
	_, _ = dbPool.Exec(ctx,
		"UPDATE siem_alerts SET task_id = $1, status = 'investigating' WHERE id = $2",
		taskID, alertID,
	)

	// Audit log for auto-investigate action (use json.Marshal to prevent JSON injection — Security H11)
	auditDetails2, _ := json.Marshal(map[string]string{"task_id": taskID, "alert_name": alertName})
	_, _ = dbPool.Exec(ctx,
		"INSERT INTO agent_audit_log (tenant_id, action, resource_type, resource_id, details) VALUES ($1, $2, $3, $4, $5)",
		tenantID, "auto_investigate", "siem_alert", alertID,
		string(auditDetails2),
	)

	return taskID, nil
}

// ============================================================
// LOG SOURCES CRUD
// ============================================================

func listLogSourcesHandler(c *gin.Context) {
	tenantID := c.MustGet("tenant_id").(string)

	rows, err := dbPool.Query(c.Request.Context(),
		"SELECT id, name, source_type, connection_config, is_active, last_event_at, event_count, created_at FROM log_sources WHERE tenant_id = $1 ORDER BY created_at DESC",
		tenantID,
	)
	if err != nil {
		respondInternalError(c, err, "query log sources")
		return
	}
	defer rows.Close()

	var sources []map[string]interface{}
	for rows.Next() {
		var id, name, sourceType string
		var connConfig map[string]interface{}
		var isActive bool
		var lastEventAt *time.Time
		var eventCount int
		var createdAt time.Time

		if err := rows.Scan(&id, &name, &sourceType, &connConfig, &isActive, &lastEventAt, &eventCount, &createdAt); err != nil {
			log.Printf("Error scanning log source row: %v", err)
			continue
		}

		source := map[string]interface{}{
			"id":                id,
			"name":              name,
			"source_type":       sourceType,
			"connection_config": connConfig,
			"is_active":         isActive,
			"last_event_at":     lastEventAt,
			"event_count":       eventCount,
			"created_at":        createdAt,
			"webhook_url":       fmt.Sprintf("/api/v1/webhooks/%s/alert", id),
		}
		sources = append(sources, source)
	}

	if sources == nil {
		sources = []map[string]interface{}{}
	}

	c.JSON(http.StatusOK, gin.H{"sources": sources, "count": len(sources)})
}

func createLogSourceHandler(c *gin.Context) {
	tenantID := c.MustGet("tenant_id").(string)
	userID := c.MustGet("user_id").(string)

	var req struct {
		Name             string                 `json:"name" binding:"required"`
		SourceType       string                 `json:"source_type" binding:"required"`
		ConnectionConfig map[string]interface{} `json:"connection_config"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	if req.ConnectionConfig == nil {
		req.ConnectionConfig = map[string]interface{}{}
	}

	sourceID := uuid.New().String()
	configJSON, _ := json.Marshal(req.ConnectionConfig)

	_, err := dbPool.Exec(c.Request.Context(),
		"INSERT INTO log_sources (id, tenant_id, name, source_type, connection_config, created_by) VALUES ($1, $2, $3, $4, $5, $6)",
		sourceID, tenantID, req.Name, req.SourceType, configJSON, userID,
	)
	if err != nil {
		respondInternalError(c, err, "create log source")
		return
	}

	c.JSON(http.StatusCreated, gin.H{
		"id":          sourceID,
		"name":        req.Name,
		"source_type": req.SourceType,
		"webhook_url": fmt.Sprintf("/api/v1/webhooks/%s/alert", sourceID),
		"is_active":   true,
	})
}

func updateLogSourceHandler(c *gin.Context) {
	sourceID := c.Param("id")
	tenantID := c.MustGet("tenant_id").(string)

	var req struct {
		Name             *string                `json:"name"`
		ConnectionConfig map[string]interface{} `json:"connection_config"`
		IsActive         *bool                  `json:"is_active"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	// Verify ownership
	var exists bool
	_ = dbPool.QueryRow(c.Request.Context(),
		"SELECT EXISTS(SELECT 1 FROM log_sources WHERE id = $1 AND tenant_id = $2)", sourceID, tenantID,
	).Scan(&exists)
	if !exists {
		c.JSON(http.StatusNotFound, gin.H{"error": "source not found"})
		return
	}

	if req.Name != nil {
		_, _ = dbPool.Exec(c.Request.Context(), "UPDATE log_sources SET name = $1, updated_at = NOW() WHERE id = $2", *req.Name, sourceID)
	}
	if req.ConnectionConfig != nil {
		configJSON, _ := json.Marshal(req.ConnectionConfig)
		_, _ = dbPool.Exec(c.Request.Context(), "UPDATE log_sources SET connection_config = $1, updated_at = NOW() WHERE id = $2", configJSON, sourceID)
	}
	if req.IsActive != nil {
		_, _ = dbPool.Exec(c.Request.Context(), "UPDATE log_sources SET is_active = $1, updated_at = NOW() WHERE id = $2", *req.IsActive, sourceID)
	}

	c.JSON(http.StatusOK, gin.H{"status": "updated"})
}

func deleteLogSourceHandler(c *gin.Context) {
	sourceID := c.Param("id")
	tenantID := c.MustGet("tenant_id").(string)

	result, err := dbPool.Exec(c.Request.Context(),
		"UPDATE log_sources SET is_active = false, updated_at = NOW() WHERE id = $1 AND tenant_id = $2",
		sourceID, tenantID,
	)
	if err != nil {
		respondInternalError(c, err, "deactivate log source")
		return
	}
	if result.RowsAffected() == 0 {
		c.JSON(http.StatusNotFound, gin.H{"error": "source not found"})
		return
	}

	c.JSON(http.StatusOK, gin.H{"status": "deactivated"})
}

// ============================================================
// SIEM ALERTS LIST
// ============================================================

func listSIEMalertsHandler(c *gin.Context) {
	tenantID := c.MustGet("tenant_id").(string)

	query := `SELECT id, log_source_id, task_id, alert_name, severity, source_ip, dest_ip, rule_name, status, auto_investigate, created_at
		FROM siem_alerts WHERE tenant_id = $1`
	args := []interface{}{tenantID}
	argN := 2

	if status := c.Query("status"); status != "" {
		query += fmt.Sprintf(" AND status = $%d", argN)
		args = append(args, status)
		argN++
	}
	if sourceID := c.Query("source_id"); sourceID != "" {
		query += fmt.Sprintf(" AND log_source_id = $%d", argN)
		args = append(args, sourceID)
		argN++
	}

	query += " ORDER BY created_at DESC LIMIT 50"

	rows, err := dbPool.Query(c.Request.Context(), query, args...)
	if err != nil {
		respondInternalError(c, err, "query SIEM alerts")
		return
	}
	defer rows.Close()

	var alerts []map[string]interface{}
	for rows.Next() {
		var id, logSourceID, alertName, status string
		var taskID, severity, sourceIP, destIP, ruleName *string
		var autoInvestigate bool
		var createdAt time.Time

		if err := rows.Scan(&id, &logSourceID, &taskID, &alertName, &severity, &sourceIP, &destIP, &ruleName, &status, &autoInvestigate, &createdAt); err != nil {
			log.Printf("Error scanning alert row: %v", err)
			continue
		}

		alert := map[string]interface{}{
			"id":               id,
			"log_source_id":    logSourceID,
			"task_id":          taskID,
			"alert_name":       alertName,
			"severity":         severity,
			"source_ip":        sourceIP,
			"dest_ip":          destIP,
			"rule_name":        ruleName,
			"status":           status,
			"auto_investigate": autoInvestigate,
			"created_at":       createdAt,
		}
		alerts = append(alerts, alert)
	}

	if alerts == nil {
		alerts = []map[string]interface{}{}
	}

	c.JSON(http.StatusOK, gin.H{"alerts": alerts, "count": len(alerts)})
}

// Investigate a specific alert (manually triggered from UI)
func investigateAlertHandler(c *gin.Context) {
	alertID := c.Param("id")
	tenantID := c.MustGet("tenant_id").(string)

	// Get the alert (with 5s lock timeout)
	var alertName, severity, status string
	var sourceIP, destIP, ruleName *string
	var normEvent map[string]interface{}
	var rawEvent []byte

	dbCtx, dbCancel := dbContextWithTimeout(c.Request.Context())
	defer dbCancel()

	err := dbPool.QueryRow(dbCtx,
		"SELECT alert_name, severity, source_ip, dest_ip, rule_name, normalized_event, raw_event, status FROM siem_alerts WHERE id = $1 AND tenant_id = $2",
		alertID, tenantID,
	).Scan(&alertName, &severity, &sourceIP, &destIP, &ruleName, &normEvent, &rawEvent, &status)
	if err != nil {
		if isLockTimeout(err) {
			HandlePostgresLock(c, tenantID, alertID, "unknown", "SELECT", "siem_alerts", 5000)
			return
		}
		c.JSON(http.StatusNotFound, gin.H{"error": "alert not found"})
		return
	}

	if status != "new" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "alert already being investigated or resolved"})
		return
	}

	// Validate SIEM source credentials before starting investigation
	var logSourceID string
	var connConfig map[string]interface{}
	srcErr := dbPool.QueryRow(c.Request.Context(),
		`SELECT sa.log_source_id, ls.connection_config
		 FROM siem_alerts sa JOIN log_sources ls ON sa.log_source_id = ls.id
		 WHERE sa.id = $1`, alertID,
	).Scan(&logSourceID, &connConfig)
	if srcErr == nil {
		if tokenExpiry, ok := connConfig["token_expiry"].(string); ok && tokenExpiry != "" {
			expTime, parseErr := time.Parse(time.RFC3339, tokenExpiry)
			if parseErr == nil && expTime.Before(time.Now()) {
				priority := severity
				if priority == "" {
					priority = "medium"
				}
				// Credentials expired — block the investigation
				taskID := uuid.New().String()
				_, _ = dbPool.Exec(c.Request.Context(),
					"INSERT INTO agent_tasks (id, tenant_id, task_type, input, status, created_at) VALUES ($1, $2, $3, $4, $5, $6)",
					taskID, tenantID, "incident_response", normEvent, StatusBlockedCredentials, time.Now(),
				)
				siemEndpoint := fmt.Sprintf("log_source:%s", logSourceID)
				HandleTelemetryAccessDenied(c.Request.Context(), tenantID, taskID, priority,
					siemEndpoint, 401, tokenExpiry)
				c.JSON(http.StatusUnprocessableEntity, gin.H{
					"error":        "SIEM credentials expired. Credential rotation webhook dispatched.",
					"failure_mode": FailureTelemetryDenied,
					"task_id":      taskID,
					"status":       StatusBlockedCredentials,
				})
				return
			}
		}
	}

	ocsf := ocsfFromStoredNormalized(normEvent)
	taskID, err := autoInvestigateAlert(c.Request.Context(), tenantID, alertID, ocsf, rawEvent)
	if err != nil {
		respondInternalError(c, err, "start investigation")
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"alert_id":         alertID,
		"investigation_id": taskID,
		"status":           "investigating",
	})
}
