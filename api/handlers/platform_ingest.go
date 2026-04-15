// Package handlers holds HTTP handlers wired from the API main package.
//
// Verify platform ingest (HTTPS → Redpanda raw.training-data.{customer_id}):
//
//	curl -sS -o /dev/null -w "%{http_code}" \
//	  -H "Authorization: Bearer $ZOVARK_PLATFORM_INGEST_BEARER" \
//	  -H "X-Customer-ID: $CUSTOMER_UUID" \
//	  --data-binary @export.parquet \
//	  http://api:8090/api/v1/platform/training-data/ingest
//
// Expect: 204
package handlers

import (
	"context"
	"crypto/subtle"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

const (
	// SystemTenantUUID is the platform default when no customer header is present.
	SystemTenantUUID = "00000000-0000-0000-0000-000000000001"
	// IngestRatePerMinute caps successful ingests per customer per UTC minute.
	IngestRatePerMinute int64 = 1000
)

var customerIDPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]{6,127}$`)

// PlatformIngestDeps wires Redis rate limiting and Redpanda publish from package main.
type PlatformIngestDeps struct {
	// BearerSecret from ZOVARK_PLATFORM_INGEST_BEARER (required when handler runs).
	BearerSecret string
	// MaxBodyBytes caps raw Parquet/binary payload (default 32MiB if 0).
	MaxBodyBytes int64
	// Redis for rate:ingest:{customer_id}:{utc_minute}; if nil, AllowIngest must be set.
	Redis redis.Cmdable
	// AllowIngest optional override (tests); if nil, AllowIngestPerMinuteRedis is used with Redis.
	AllowIngest func(ctx context.Context, customerID string) (bool, error)
	// Publish forwards raw body to Redpanda topic raw.training-data.{customer_id}.
	Publish func(ctx context.Context, customerID string, body []byte) error
}

// ResolveCustomerID prefers X-Customer-ID, then X-Zovark-Customer-Id, then system tenant.
func ResolveCustomerID(c *gin.Context) string {
	for _, h := range []string{"X-Customer-ID", "X-Customer-Id", "X-Zovark-Customer-Id"} {
		v := strings.TrimSpace(c.GetHeader(h))
		if v != "" && customerIDPattern.MatchString(v) {
			return v
		}
	}
	return SystemTenantUUID
}

// AllowIngestPerMinuteRedis enforces up to `limit` requests per UTC minute per customer.
// Redis key: rate:ingest:{customer_id}:{yyyyMMddHHmm}
func AllowIngestPerMinuteRedis(ctx context.Context, rdb redis.Cmdable, customerID string, limit int64) (bool, error) {
	if rdb == nil {
		return false, fmt.Errorf("redis client required for ingest rate limit")
	}
	minute := time.Now().UTC().Format("200601021504")
	key := fmt.Sprintf("rate:ingest:%s:%s", customerID, minute)
	n, err := rdb.Incr(ctx, key).Result()
	if err != nil {
		return false, err
	}
	if n == 1 {
		_ = rdb.Expire(ctx, key, 2*time.Minute).Err()
	}
	return n <= limit, nil
}

// PlatformTrainingIngest returns a Gin handler: Bearer auth, per-customer rate limit,
// raw body → Redpanda, HTTP 204 (no JSON parse of body).
func PlatformTrainingIngest(deps PlatformIngestDeps) gin.HandlerFunc {
	maxBody := deps.MaxBodyBytes
	if maxBody <= 0 {
		maxBody = 32 << 20
	}
	allow := deps.AllowIngest
	if allow == nil && deps.Redis != nil {
		allow = func(ctx context.Context, customerID string) (bool, error) {
			return AllowIngestPerMinuteRedis(ctx, deps.Redis, customerID, IngestRatePerMinute)
		}
	}
	if allow == nil {
		allow = func(ctx context.Context, customerID string) (bool, error) {
			return false, fmt.Errorf("redis not configured for platform ingest rate limit")
		}
	}

	return func(c *gin.Context) {
		secret := strings.TrimSpace(deps.BearerSecret)
		if secret == "" {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "platform ingest disabled", "code": "INGEST_DISABLED"})
			return
		}

		auth := strings.TrimSpace(c.GetHeader("Authorization"))
		const p = "Bearer "
		if !strings.HasPrefix(auth, p) {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid or missing bearer token"})
			return
		}
		provided := strings.TrimSpace(auth[len(p):])
		// Constant-time comparison — prevents timing side-channel on the platform ingest token.
		if subtle.ConstantTimeCompare([]byte(provided), []byte(secret)) != 1 {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid or missing bearer token"})
			return
		}

		customerID := ResolveCustomerID(c)
		if customerID == "" || !customerIDPattern.MatchString(customerID) {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid customer id"})
			return
		}

		ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
		defer cancel()

		ok, err := allow(ctx, customerID)
		if err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "rate limit backend unavailable"})
			return
		}
		if !ok {
			c.Header("Retry-After", "60")
			c.JSON(http.StatusTooManyRequests, gin.H{"error": "ingest rate limit exceeded", "code": "RATE_LIMIT_EXCEEDED"})
			return
		}

		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxBody)
		body, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request body too large"})
			return
		}
		if len(body) == 0 {
			c.JSON(http.StatusBadRequest, gin.H{"error": "empty body"})
			return
		}

		if deps.Publish == nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal error"})
			return
		}
		if err := deps.Publish(ctx, customerID, body); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal error"})
			return
		}

		c.Status(http.StatusNoContent)
	}
}
