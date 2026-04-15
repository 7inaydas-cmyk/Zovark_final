package main

import (
	"encoding/json"
	"io"
	"net/http/httptest"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
)

func init() {
	gin.SetMode(gin.TestMode)
	// Override the config initialised by the package-level init() in main.go so
	// that unit tests never need a 32-char JWT_SECRET env var and never try to
	// talk to a real database / Redis / Temporal.
	appConfig = &Config{
		JWTSecret: "test-secret-that-is-at-least-32-characters-long",
	}
}

// setupTestRouter builds a minimal Gin router that exercises the middleware
// chain and RBAC logic without requiring a live database, Redis, or Temporal
// connection.  Middlewares that call external services (tenantRateLimitMiddleware,
// auditMiddleware, loggingMiddleware) are intentionally omitted so that tests
// are hermetic.
func setupTestRouter() *gin.Engine {
	router := gin.New()
	router.Use(gin.Recovery()) // Converts panics (e.g. c.MustGet on missing key) to 500
	router.Use(corsMiddleware())
	router.Use(securityHeadersMiddleware())
	router.Use(maxBodySizeMiddleware(10 << 20))

	// Public routes
	router.GET("/health", healthCheckHandler)

	// Public auth routes (rate-limited)
	auth := router.Group("/api/v1/auth")
	auth.Use(authRateLimitMiddleware())
	{
		auth.POST("/login", loginHandler)
		auth.POST("/register", registerHandler)
		auth.POST("/refresh", refreshHandler)
		auth.POST("/logout", logoutHandler)
	}

	// Protected API routes — auth middleware only (no DB-dependent middlewares)
	api := router.Group("/api/v1")
	api.Use(authMiddleware())
	{
		// Any authenticated caller
		api.GET("/skills", listSkillsHandler)
		api.GET("/me", getMeHandler)

		// Admin only
		api.GET("/tenants", requireRole("admin"), listTenantsHandler)
		api.POST("/tenants", requireRole("admin"), createTenantHandler)
		api.GET("/tenants/:id", requireRole("admin"), getTenantHandler)
		api.PUT("/tenants/:id", requireRole("admin"), updateTenantHandler)
		api.DELETE("/tenants/:id/data", requireRole("admin"), gdprEraseHandler)

		api.GET("/models", requireRole("admin"), listModelsHandler)
		api.PUT("/models/:id", requireRole("admin"), updateModelHandler)

		api.POST("/admin/diagnostics/probe-db", requireRole("admin"), probeDBHandler)
	}

	return router
}

// createTestJWT mints a valid HS256 access token (subject "access") signed
// with the test secret.
func createTestJWT(tenantID, userID, email, role string) string {
	claims := CustomClaims{
		TenantID: tenantID,
		UserID:   userID,
		Email:    email,
		Role:     role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(15 * time.Minute)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
			Subject:   "access",
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	tokenString, _ := token.SignedString([]byte(appConfig.JWTSecret))
	return tokenString
}

// createExpiredJWT mints an already-expired HS256 token.
func createExpiredJWT(tenantID, userID, role string) string {
	claims := CustomClaims{
		TenantID: tenantID,
		UserID:   userID,
		Role:     role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(-1 * time.Hour)),
			IssuedAt:  jwt.NewNumericDate(time.Now().Add(-2 * time.Hour)),
			Subject:   "access",
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	tokenString, _ := token.SignedString([]byte(appConfig.JWTSecret))
	return tokenString
}

// makeRequest performs an HTTP request against the provided router.
// When token is non-empty it is sent as the Authorization: Bearer header,
// which is what authMiddleware() reads (not a cookie).
func makeRequest(router *gin.Engine, method, path string, body interface{}, token string) *httptest.ResponseRecorder {
	var reqBody io.Reader
	if body != nil {
		jsonBytes, _ := json.Marshal(body)
		reqBody = strings.NewReader(string(jsonBytes))
	}
	req := httptest.NewRequest(method, path, reqBody)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	return w
}

// parseJSON decodes the response body into a generic map for assertion
// convenience.
func parseJSON(w *httptest.ResponseRecorder) map[string]interface{} {
	var result map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &result)
	return result
}
