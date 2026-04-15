package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

// ============================================================
// SECURITY HEADERS
// ============================================================

func securityHeadersMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Header("X-Content-Type-Options", "nosniff")
		c.Header("X-Frame-Options", "DENY")
		c.Header("X-XSS-Protection", "1; mode=block")
		c.Header("Referrer-Policy", "strict-origin-when-cross-origin")
		c.Header("Cache-Control", "no-store, no-cache, must-revalidate")
		c.Header("Pragma", "no-cache")
		// CSP and HSTS (Security P2#26)
		c.Header("Content-Security-Policy",
			"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "+
				"img-src 'self' data:; font-src 'self'; connect-src 'self'; "+
				"frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
		c.Header("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
		c.Header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		c.Next()
	}
}

// ============================================================
// MAX BODY SIZE MIDDLEWARE
// ============================================================

func maxBodySizeMiddleware(maxBytes int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		if c.Request.Body != nil {
			c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxBytes)
		}
		c.Next()
	}
}

// ============================================================
// AUTH RATE LIMITING (in-memory, per-IP)
// ============================================================

type rateLimiter struct {
	mu       sync.Mutex
	attempts map[string][]time.Time
	window   time.Duration
	limit    int
}

var authLimiter = &rateLimiter{
	attempts: make(map[string][]time.Time),
	window:   15 * time.Minute,
	limit:    10, // 10 attempts per 15 minutes per IP
}

// authLimiterSweepOnce deletes entries whose latest timestamp is older than
// the rate-limit window. Called periodically by a background goroutine so the
// map doesn't grow unbounded with stale IPs. Audit 1.16.
func (rl *rateLimiter) sweepOnce() (before, after int) {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	before = len(rl.attempts)
	cutoff := time.Now().Add(-rl.window)
	for k, ts := range rl.attempts {
		keep := ts[:0]
		for _, t := range ts {
			if t.After(cutoff) {
				keep = append(keep, t)
			}
		}
		if len(keep) == 0 {
			delete(rl.attempts, k)
		} else {
			rl.attempts[k] = keep
		}
	}
	after = len(rl.attempts)
	return
}

// startAuthLimiterSweeper runs rl.sweepOnce() every 60 seconds until ctx is
// cancelled. Wrap with safeGoroutine() so a panic can't take the API down.
// Audit 1.16.
func startAuthLimiterSweeper(ctx context.Context) {
	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			before, after := authLimiter.sweepOnce()
			if before != after {
				log.Printf("[authLimiter] sweep: %d → %d entries", before, after)
			}
		}
	}
}

func (rl *rateLimiter) allow(key string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()

	now := time.Now()
	cutoff := now.Add(-rl.window)

	// Clean old entries
	var valid []time.Time
	for _, t := range rl.attempts[key] {
		if t.After(cutoff) {
			valid = append(valid, t)
		}
	}
	rl.attempts[key] = valid

	if len(valid) >= rl.limit {
		return false
	}

	rl.attempts[key] = append(rl.attempts[key], now)
	return true
}

func authRateLimitMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		ip := c.ClientIP()
		if !authLimiter.allow(ip) {
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
				"error": "Too many authentication attempts. Try again later.",
			})
			return
		}
		c.Next()
	}
}

// ============================================================
// AUDIT MIDDLEWARE — logs all mutating API calls
// ============================================================

func auditMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Next()

		// Only audit mutating requests that succeeded
		method := c.Request.Method
		if method != "POST" && method != "PUT" && method != "DELETE" {
			return
		}
		if c.Writer.Status() >= 400 {
			return
		}

		tenantID, _ := c.Get("tenant_id")
		userID, _ := c.Get("user_id")

		action := fmt.Sprintf("api_%s_%s", method, c.FullPath())
		detailsBytes, _ := json.Marshal(map[string]interface{}{"method": method, "path": c.Request.URL.Path, "status": c.Writer.Status(), "ip": c.ClientIP()})
		details := string(detailsBytes)

		go func() {
			_, err := dbPool.Exec(context.Background(),
				"INSERT INTO agent_audit_log (tenant_id, action, resource_type, resource_id, details) VALUES ($1, $2, $3, $4, $5)",
				tenantID, action, "api", userID, details,
			)
			if err != nil {
				log.Printf("Audit log failed: %v", err)
			}
		}()
	}
}

// ============================================================
// ACCOUNT LOCKOUT
//
// Audit 1.15: the three operations used to be independent UPDATE + SELECT +
// optional UPDATE statements on context.Background(). Two concurrent failed
// logins could both read failed_login_attempts=4 and neither would trip the
// lockout threshold, letting the counter race past 5 silently. The unified
// path below uses a single `UPDATE ... RETURNING` per event so the increment
// and the threshold check are atomic, and every call now propagates the
// request context for tracing/cancellation.
// ============================================================

const lockoutThreshold = 5
const lockoutDuration = 30 * time.Minute

func checkAccountLocked(ctx context.Context, email string) bool {
	var lockedUntil *time.Time
	err := dbPool.QueryRow(ctx,
		"SELECT locked_until FROM users WHERE email = $1", email,
	).Scan(&lockedUntil)
	if err != nil {
		return false
	}
	return lockedUntil != nil && lockedUntil.After(time.Now())
}

// recordFailedLogin atomically bumps the fail counter and, if the new count
// crosses the threshold, sets locked_until in the same statement. A second
// concurrent failure racing the first is serialized by the row lock taken
// inside the UPDATE.
func recordFailedLogin(ctx context.Context, email string) {
	const q = `
		UPDATE users
		   SET failed_login_attempts = failed_login_attempts + 1,
		       locked_until = CASE
		           WHEN failed_login_attempts + 1 >= $2
		           THEN NOW() + ($3 || ' seconds')::interval
		           ELSE locked_until
		       END
		 WHERE email = $1
		RETURNING failed_login_attempts, locked_until`
	var attempts int
	var lockedUntil *time.Time
	err := dbPool.QueryRow(ctx, q,
		email,
		lockoutThreshold,
		int(lockoutDuration.Seconds()),
	).Scan(&attempts, &lockedUntil)
	if err != nil {
		// User may not exist (email-enumeration path) — swallow silently.
		return
	}
	if lockedUntil != nil && attempts >= lockoutThreshold {
		log.Printf("[lockout] account locked: email=%s attempts=%d until=%s",
			email, attempts, lockedUntil.Format(time.RFC3339))
	}
}

func recordSuccessfulLogin(ctx context.Context, email string) {
	_, _ = dbPool.Exec(ctx,
		"UPDATE users SET failed_login_attempts = 0, locked_until = NULL, last_login_at = NOW() WHERE email = $1", email)
}

// ============================================================
// DATA RETENTION
// ============================================================

func listRetentionPoliciesHandler(c *gin.Context) {
	tenantID := c.MustGet("tenant_id").(string)

	rows, err := dbPool.Query(c.Request.Context(),
		"SELECT id, table_name, retention_days, delete_strategy, is_active, last_cleanup_at, rows_cleaned FROM data_retention_policies WHERE tenant_id = $1 ORDER BY table_name",
		tenantID,
	)
	if err != nil {
		respondInternalError(c, err, "query retention policies")
		return
	}
	defer rows.Close()

	var policies []map[string]interface{}
	for rows.Next() {
		var id, tableName, strategy string
		var retentionDays, rowsCleaned int
		var isActive bool
		var lastCleanup *time.Time

		if err := rows.Scan(&id, &tableName, &retentionDays, &strategy, &isActive, &lastCleanup, &rowsCleaned); err != nil {
			continue
		}

		policies = append(policies, map[string]interface{}{
			"id":              id,
			"table_name":      tableName,
			"retention_days":  retentionDays,
			"delete_strategy": strategy,
			"is_active":       isActive,
			"last_cleanup_at": lastCleanup,
			"rows_cleaned":    rowsCleaned,
		})
	}

	if policies == nil {
		policies = []map[string]interface{}{}
	}

	c.JSON(http.StatusOK, gin.H{"policies": policies, "count": len(policies)})
}

func updateRetentionPolicyHandler(c *gin.Context) {
	policyID := c.Param("id")
	tenantID := c.MustGet("tenant_id").(string)

	var req struct {
		RetentionDays *int  `json:"retention_days"`
		IsActive      *bool `json:"is_active"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	if req.RetentionDays != nil {
		_, _ = dbPool.Exec(c.Request.Context(),
			"UPDATE data_retention_policies SET retention_days = $1 WHERE id = $2 AND tenant_id = $3",
			*req.RetentionDays, policyID, tenantID)
	}
	if req.IsActive != nil {
		_, _ = dbPool.Exec(c.Request.Context(),
			"UPDATE data_retention_policies SET is_active = $1 WHERE id = $2 AND tenant_id = $3",
			*req.IsActive, policyID, tenantID)
	}

	c.JSON(http.StatusOK, gin.H{"status": "updated"})
}
