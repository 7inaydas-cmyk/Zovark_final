package main

import (
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

// --------------------------------------------------------------------------
// Cipher Audit API — Zovark Sprint 2C
//
// Provides 5 endpoints for TLS/SSL cipher audit visibility:
//   GET  /api/v1/cipher-audit/stats    — aggregate risk counts
//   GET  /api/v1/cipher-audit/summary  — PFS% per server (materialized view)
//   GET  /api/v1/cipher-audit/findings — paginated non-secure findings
//   GET  /api/v1/cipher-audit/servers  — distinct servers + worst risk
//   POST /api/v1/cipher-audit/analyze  — submit entries for deterministic risk
// --------------------------------------------------------------------------

// cipherAuditStatsHandler returns aggregate risk-level counts for the tenant.
// GET /api/v1/cipher-audit/stats
func cipherAuditStatsHandler(c *gin.Context) {
	ctx := c.Request.Context()
	tenantID, _ := c.Get("tenant_id")
	tid, _ := tenantID.(string)

	if dbPool == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "database unavailable"})
		return
	}

	var critical, warning, secure int64
	row := dbPool.QueryRow(ctx, `
		SELECT
			COUNT(*) FILTER (WHERE risk_level = 'critical'),
			COUNT(*) FILTER (WHERE risk_level = 'warning'),
			COUNT(*) FILTER (WHERE risk_level = 'secure')
		FROM cipher_audit_events
		WHERE tenant_id = $1`, tid)

	if err := row.Scan(&critical, &warning, &secure); err != nil {
		// Table may not exist yet — return zeros gracefully
		c.JSON(http.StatusOK, gin.H{
			"critical": 0,
			"warning":  0,
			"secure":   0,
			"total":    0,
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"critical": critical,
		"warning":  warning,
		"secure":   secure,
		"total":    critical + warning + secure,
	})
}

// cipherAuditSummaryHandler returns PFS% per server from the materialized view.
// GET /api/v1/cipher-audit/summary
func cipherAuditSummaryHandler(c *gin.Context) {
	ctx := c.Request.Context()
	tenantID, _ := c.Get("tenant_id")
	tid, _ := tenantID.(string)

	if dbPool == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "database unavailable"})
		return
	}

	rows, err := dbPool.Query(ctx, `
		SELECT server_hostname, audit_date,
			   total_connections, pfs_connections, pfs_percentage,
			   critical_count, warning_count, min_security_bits
		FROM cipher_audit_summary
		WHERE tenant_id = $1
		ORDER BY audit_date DESC, server_hostname ASC
		LIMIT 200`, tid)
	if err != nil {
		if isRelationMissing(err) {
			c.JSON(http.StatusOK, gin.H{"servers": []interface{}{}, "count": 0})
			return
		}
		respondInternalError(c, err, "query cipher audit summary")
		return
	}
	defer rows.Close()

	var servers []map[string]interface{}
	for rows.Next() {
		var server string
		var auditDate interface{}
		var totalConns, pfsConns, criticalCount, warningCount int64
		var pfsPercentage float64
		var minBits *int

		if err := rows.Scan(&server, &auditDate, &totalConns, &pfsConns,
			&pfsPercentage, &criticalCount, &warningCount, &minBits); err != nil {
			respondInternalError(c, err, "scan cipher audit summary row")
			return
		}

		entry := map[string]interface{}{
			"server":            server,
			"audit_date":        auditDate,
			"total_connections": totalConns,
			"pfs_connections":   pfsConns,
			"pfs_percentage":    pfsPercentage,
			"critical_count":    criticalCount,
			"warning_count":     warningCount,
			"min_security_bits": minBits,
		}
		servers = append(servers, entry)
	}

	if servers == nil {
		servers = []map[string]interface{}{}
	}

	c.JSON(http.StatusOK, gin.H{
		"servers": servers,
		"count":   len(servers),
	})
}

// cipherAuditFindingsHandler returns paginated non-secure findings.
// GET /api/v1/cipher-audit/findings?limit=50&offset=0&risk=critical
func cipherAuditFindingsHandler(c *gin.Context) {
	ctx := c.Request.Context()
	tenantID, _ := c.Get("tenant_id")
	tid, _ := tenantID.(string)

	if dbPool == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "database unavailable"})
		return
	}

	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "50"))
	offset, _ := strconv.Atoi(c.DefaultQuery("offset", "0"))
	riskFilter := c.Query("risk")

	if limit <= 0 || limit > 200 {
		limit = 50
	}
	if offset < 0 {
		offset = 0
	}

	query := `
		SELECT id, server_hostname, client_ip, ssl_protocol, ssl_cipher,
		       observed_at, risk_level, has_pfs, security_bits,
		       vulnerability_class, affected_component, raw_finding,
		       remediation_steps, llm_headline, mitre_techniques, created_at
		FROM cipher_audit_events
		WHERE tenant_id = $1
		  AND risk_level IN ('critical', 'warning')
	`
	args := []interface{}{tid}
	argIdx := 2

	if riskFilter == "critical" || riskFilter == "warning" {
		query += fmt.Sprintf(" AND risk_level = $%d", argIdx)
		args = append(args, riskFilter)
		argIdx++
	}

	query += fmt.Sprintf(" ORDER BY observed_at DESC LIMIT $%d OFFSET $%d", argIdx, argIdx+1)
	args = append(args, limit, offset)

	rows, err := dbPool.Query(ctx, query, args...)
	if err != nil {
		if isRelationMissing(err) {
			c.JSON(http.StatusOK, gin.H{"findings": []interface{}{}, "count": 0, "limit": limit, "offset": offset})
			return
		}
		respondInternalError(c, err, "query cipher audit findings")
		return
	}
	defer rows.Close()

	var findings []map[string]interface{}
	for rows.Next() {
		var id, server, protocol, cipher, riskLevel string
		var clientIP, vulnClass, affectedComp, rawFinding, headline *string
		var observedAt, createdAt interface{}
		var hasPFS bool
		var secBits *int
		var remediation, mitreTech interface{}

		if err := rows.Scan(&id, &server, &clientIP, &protocol, &cipher,
			&observedAt, &riskLevel, &hasPFS, &secBits,
			&vulnClass, &affectedComp, &rawFinding,
			&remediation, &headline, &mitreTech, &createdAt); err != nil {
			respondInternalError(c, err, "scan cipher audit finding row")
			return
		}

		finding := map[string]interface{}{
			"id":                  id,
			"server_hostname":    server,
			"client_ip":          clientIP,
			"ssl_protocol":       protocol,
			"ssl_cipher":         cipher,
			"observed_at":        observedAt,
			"risk_level":         riskLevel,
			"has_pfs":            hasPFS,
			"security_bits":      secBits,
			"vulnerability_class": vulnClass,
			"affected_component": affectedComp,
			"raw_finding":        rawFinding,
			"remediation_steps":  remediation,
			"llm_headline":       headline,
			"mitre_techniques":   mitreTech,
			"created_at":         createdAt,
		}
		findings = append(findings, finding)
	}

	if findings == nil {
		findings = []map[string]interface{}{}
	}

	c.JSON(http.StatusOK, gin.H{
		"findings": findings,
		"count":    len(findings),
		"limit":    limit,
		"offset":   offset,
	})
}

// cipherAuditServersHandler returns distinct servers with their worst risk level.
// GET /api/v1/cipher-audit/servers
func cipherAuditServersHandler(c *gin.Context) {
	ctx := c.Request.Context()
	tenantID, _ := c.Get("tenant_id")
	tid, _ := tenantID.(string)

	if dbPool == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "database unavailable"})
		return
	}

	rows, err := dbPool.Query(ctx, `
		SELECT server_hostname,
			COUNT(*) AS total_events,
			COUNT(*) FILTER (WHERE risk_level = 'critical') AS critical_count,
			COUNT(*) FILTER (WHERE risk_level = 'warning') AS warning_count,
			COUNT(*) FILTER (WHERE risk_level = 'secure') AS secure_count,
			CASE
				WHEN COUNT(*) FILTER (WHERE risk_level = 'critical') > 0 THEN 'critical'
				WHEN COUNT(*) FILTER (WHERE risk_level = 'warning') > 0 THEN 'warning'
				ELSE 'secure'
			END AS worst_risk,
			MIN(observed_at) AS first_seen,
			MAX(observed_at) AS last_seen
		FROM cipher_audit_events
		WHERE tenant_id = $1
		GROUP BY server_hostname
		ORDER BY
			CASE
				WHEN COUNT(*) FILTER (WHERE risk_level = 'critical') > 0 THEN 0
				WHEN COUNT(*) FILTER (WHERE risk_level = 'warning') > 0 THEN 1
				ELSE 2
			END,
			server_hostname ASC
		LIMIT 100`, tid)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{"servers": []interface{}{}, "count": 0})
		return
	}
	defer rows.Close()

	var servers []map[string]interface{}
	for rows.Next() {
		var server, worstRisk string
		var total, critical, warning, secure int64
		var firstSeen, lastSeen interface{}

		if err := rows.Scan(&server, &total, &critical, &warning, &secure,
			&worstRisk, &firstSeen, &lastSeen); err != nil {
			respondInternalError(c, err, "scan cipher audit server row")
			return
		}

		entry := map[string]interface{}{
			"server_hostname": server,
			"total_events":    total,
			"critical_count":  critical,
			"warning_count":   warning,
			"secure_count":    secure,
			"worst_risk":      worstRisk,
			"first_seen":      firstSeen,
			"last_seen":       lastSeen,
		}
		servers = append(servers, entry)
	}

	if servers == nil {
		servers = []map[string]interface{}{}
	}

	c.JSON(http.StatusOK, gin.H{
		"servers": servers,
		"count":   len(servers),
	})
}

// --------------------------------------------------------------------------
// POST /api/v1/cipher-audit/analyze — submit cipher entries for analysis
// --------------------------------------------------------------------------

// cipherAuditEntry represents a single TLS connection observation.
type cipherAuditEntry struct {
	ServerHostname string `json:"server_hostname" binding:"required"`
	ClientIP       string `json:"client_ip"`
	SSLProtocol    string `json:"ssl_protocol" binding:"required"`
	SSLCipher      string `json:"ssl_cipher" binding:"required"`
	ObservedAt     string `json:"observed_at"`
	SecurityBits   *int   `json:"security_bits"`
}

// cipherAuditAnalyzeRequest is the JSON body for the analyze endpoint.
type cipherAuditAnalyzeRequest struct {
	Entries []cipherAuditEntry `json:"entries" binding:"required,min=1"`
}

// cipherAuditAnalyzeHandler accepts cipher entries, performs deterministic risk
// classification, and inserts them into cipher_audit_events.
// POST /api/v1/cipher-audit/analyze
func cipherAuditAnalyzeHandler(c *gin.Context) {
	ctx := c.Request.Context()
	tenantID, _ := c.Get("tenant_id")
	tid, _ := tenantID.(string)

	if dbPool == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "database unavailable"})
		return
	}

	var req cipherAuditAnalyzeRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body: entries array required"})
		return
	}

	if len(req.Entries) > 1000 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "maximum 1000 entries per request"})
		return
	}

	var criticalCount, warningCount, secureCount int
	var inserted int

	for _, entry := range req.Entries {
		risk, hasPFS, vulnClass, affectedComp := classifyCipher(entry.SSLProtocol, entry.SSLCipher, entry.SecurityBits)

		switch risk {
		case "critical":
			criticalCount++
		case "warning":
			warningCount++
		default:
			secureCount++
		}

		observedAt := time.Now().UTC()
		if entry.ObservedAt != "" {
			if t, err := time.Parse(time.RFC3339, entry.ObservedAt); err == nil {
				observedAt = t
			}
		}

		id := uuid.New().String()
		_, err := dbPool.Exec(ctx, `
			INSERT INTO cipher_audit_events
				(id, tenant_id, server_hostname, client_ip, ssl_protocol, ssl_cipher,
				 observed_at, risk_level, has_pfs, security_bits,
				 vulnerability_class, affected_component, created_at)
			VALUES ($1, $2, $3, $4::inet, $5, $6, $7, $8, $9, $10, $11, $12, NOW())`,
			id, tid, entry.ServerHostname,
			nilIfEmpty(entry.ClientIP),
			entry.SSLProtocol, entry.SSLCipher,
			observedAt, risk, hasPFS, entry.SecurityBits,
			nilIfEmpty(vulnClass), nilIfEmpty(affectedComp))
		if err != nil {
			// Log but continue — partial success is acceptable for batch ingest
			continue
		}
		inserted++
	}

	c.JSON(http.StatusOK, gin.H{
		"inserted": inserted,
		"total":    len(req.Entries),
		"critical": criticalCount,
		"warning":  warningCount,
		"secure":   secureCount,
	})
}

// --------------------------------------------------------------------------
// Deterministic cipher risk classification
// --------------------------------------------------------------------------

// criticalProtocols lists TLS versions that are unconditionally critical.
var criticalProtocols = map[string]bool{
	"SSLv2": true, "SSLv3": true, "TLSv1": true, "TLSv1.0": true,
}

// warningProtocols lists TLS versions that warrant a warning.
var warningProtocols = map[string]bool{
	"TLSv1.1": true,
}

// criticalCiphers contains substrings that indicate a critical cipher suite.
var criticalCiphers = []string{
	"RC4", "DES", "NULL", "EXPORT", "MD5", "anon",
}

// warningCiphers contains substrings that indicate a weak cipher suite.
var warningCiphers = []string{
	"3DES", "CBC",
}

// pfsKeyExchanges lists key exchange prefixes that provide forward secrecy.
var pfsKeyExchanges = []string{
	"ECDHE", "DHE", "X25519", "X448",
}

// classifyCipher performs deterministic risk classification of a TLS connection.
// Returns (risk_level, has_pfs, vulnerability_class, affected_component).
func classifyCipher(protocol, cipher string, securityBits *int) (string, bool, string, string) {
	upperCipher := strings.ToUpper(cipher)
	hasPFS := false
	for _, prefix := range pfsKeyExchanges {
		if strings.Contains(upperCipher, strings.ToUpper(prefix)) {
			hasPFS = true
			break
		}
	}

	// Rule 1: Critical protocol versions
	if criticalProtocols[protocol] {
		return "critical", hasPFS, "deprecated_protocol", "protocol"
	}

	// Rule 2: Critical cipher suites
	for _, weak := range criticalCiphers {
		if strings.Contains(upperCipher, strings.ToUpper(weak)) {
			return "critical", hasPFS, "broken_cipher", "cipher"
		}
	}

	// Rule 3: Very low bit strength
	if securityBits != nil && *securityBits < 112 {
		return "critical", hasPFS, "insufficient_key_length", "cipher"
	}

	// Rule 4: Warning protocol versions
	if warningProtocols[protocol] {
		return "warning", hasPFS, "legacy_protocol", "protocol"
	}

	// Rule 5: Warning cipher suites (3DES, CBC modes)
	for _, weak := range warningCiphers {
		if strings.Contains(upperCipher, strings.ToUpper(weak)) {
			return "warning", hasPFS, "weak_cipher_mode", "cipher"
		}
	}

	// Rule 6: No forward secrecy
	if !hasPFS {
		return "warning", false, "no_forward_secrecy", "key_exchange"
	}

	// Rule 7: Marginal bit strength
	if securityBits != nil && *securityBits < 128 {
		return "warning", hasPFS, "marginal_key_length", "cipher"
	}

	return "secure", hasPFS, "", ""
}

// nilIfEmpty returns nil for empty strings (for nullable DB columns).
func nilIfEmpty(s string) interface{} {
	if s == "" {
		return nil
	}
	return s
}
