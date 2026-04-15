package main

import (
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
	"golang.org/x/crypto/bcrypt"
)

// bcryptCost returns the configured bcrypt cost with a minimum of 12.
// Audit 1.14: DefaultCost is 10, which is below modern guidance.
// Override via ZOVARK_BCRYPT_COST (clamped to [12, 15]).
func bcryptCost() int {
	const minCost = 12
	const maxCost = 15
	if v := strings.TrimSpace(os.Getenv("ZOVARK_BCRYPT_COST")); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			if n < minCost {
				n = minCost
			}
			if n > maxCost {
				n = maxCost
			}
			return n
		}
	}
	return minCost
}

// dummyBcryptHash is a valid bcrypt hash of a random string. It's used to run
// a constant-time bcrypt compare when an email lookup misses, eliminating the
// email-enumeration timing oracle (audit 1.14).
//
// Generated with: bcrypt.GenerateFromPassword([]byte("zovark-dummy-never-match"), 12)
// Matches no real password — deliberately chosen.
const dummyBcryptHash = "$2a$12$ZPFCbvNLwrK1.xhEYjXVTeb8LFMzu.fQj/Ff.tJnDXm3DZ0/MJdUi"

type RegisterRequest struct {
	Email       string `json:"email" binding:"required,email"`
	Password    string `json:"password" binding:"required,min=6"`
	DisplayName string `json:"display_name" binding:"required"`
	TenantID    string `json:"tenant_id" binding:"required"`
}

type LoginRequest struct {
	Email    string `json:"email" binding:"required,email"`
	Password string `json:"password" binding:"required"`
	TOTPCode string `json:"totp_code"` // Optional TOTP code for 2FA
}

// CustomClaims — Bearer JWT payload validated in middleware under OTel span
// "auth.jwt_validate" (Ticket 7).
type CustomClaims struct {
	TenantID string `json:"tenant_id"`
	UserID   string `json:"user_id"`
	Email    string `json:"email"`
	Role     string `json:"role"`
	jwt.RegisteredClaims
}

func registerHandler(c *gin.Context) {
	var req RegisterRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		slog.WarnContext(c.Request.Context(), "auth_register_failed",
			slog.String("event", "auth.register"),
			slog.String("outcome", "failure"),
			slog.String("reason", "validation_error"),
		)
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	// Validate that the tenant_id exists and is active
	var tenantActive bool
	err := dbPool.QueryRow(c.Request.Context(), // FIX #11
		"SELECT is_active FROM tenants WHERE id = $1", req.TenantID,
	).Scan(&tenantActive)
	if err != nil {
		slog.WarnContext(c.Request.Context(), "auth_register_failed",
			slog.String("event", "auth.register"),
			slog.String("outcome", "failure"),
			slog.String("reason", "invalid_tenant"),
		)
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid tenant_id"})
		return
	}
	if !tenantActive {
		slog.WarnContext(c.Request.Context(), "auth_register_failed",
			slog.String("event", "auth.register"),
			slog.String("outcome", "failure"),
			slog.String("reason", "tenant_inactive"),
			slog.String("tenant_id", req.TenantID),
		)
		c.JSON(http.StatusForbidden, gin.H{"error": "tenant is inactive"})
		return
	}

	// Audit 1.14: bcrypt cost 12 is the modern minimum. DefaultCost is 10.
	hashedPassword, err := bcrypt.GenerateFromPassword([]byte(req.Password), bcryptCost())
	if err != nil {
		respondInternalError(c, err, "hash password")
		return
	}

	userID := uuid.New().String()

	_, err = dbPool.Exec(c.Request.Context(), // FIX #11
		"INSERT INTO users (id, tenant_id, email, display_name, role, password_hash) VALUES ($1, $2, $3, $4, $5, $6)",
		userID, req.TenantID, req.Email, req.DisplayName, "analyst", string(hashedPassword))
	if err != nil {
		respondInternalError(c, err, "create user")
		return
	}

	slog.InfoContext(c.Request.Context(), "auth_register",
		slog.String("event", "auth.register"),
		slog.String("outcome", "success"),
		slog.String("user_id", userID),
		slog.String("tenant_id", req.TenantID),
	)

	c.JSON(http.StatusCreated, gin.H{
		"user": map[string]interface{}{
			"id":        userID,
			"email":     req.Email,
			"role":      "analyst",
			"tenant_id": req.TenantID,
		},
	})
}

func loginHandler(c *gin.Context) {
	var req LoginRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		slog.WarnContext(c.Request.Context(), "auth_login_failed",
			slog.String("event", "auth.login"),
			slog.String("outcome", "failure"),
			slog.String("reason", "validation_error"),
		)
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	var user struct {
		ID           string
		TenantID     string
		Email        string
		Role         string
		PasswordHash string
	}

	err := dbPool.QueryRow(c.Request.Context(), // FIX #11
		"SELECT id, tenant_id, email, role, password_hash FROM users WHERE email = $1", req.Email).
		Scan(&user.ID, &user.TenantID, &user.Email, &user.Role, &user.PasswordHash)

	// Audit 1.14: eliminate the email-enumeration timing oracle by running a
	// dummy bcrypt compare when the user row is missing. Without this, an
	// attacker measuring login latency can distinguish "user exists, wrong
	// password" (slow — bcrypt ran) from "user does not exist" (fast — miss)
	// and enumerate valid emails.
	if err != nil {
		_ = bcrypt.CompareHashAndPassword([]byte(dummyBcryptHash), []byte(req.Password))
		slog.WarnContext(c.Request.Context(), "auth_login_failed",
			slog.String("event", "auth.login"),
			slog.String("outcome", "failure"),
			slog.String("reason", "invalid_credentials"),
		)
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid credentials"})
		return
	}

	// Check account lockout
	if checkAccountLocked(c.Request.Context(), req.Email) {
		slog.WarnContext(c.Request.Context(), "auth_login_failed",
			slog.String("event", "auth.login"),
			slog.String("outcome", "failure"),
			slog.String("reason", "account_locked"),
			slog.String("user_id", user.ID),
		)
		c.JSON(http.StatusTooManyRequests, gin.H{"error": "account temporarily locked due to failed login attempts"})
		return
	}

	if err := bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(req.Password)); err != nil {
		recordFailedLogin(c.Request.Context(), req.Email)
		slog.WarnContext(c.Request.Context(), "auth_login_failed",
			slog.String("event", "auth.login"),
			slog.String("outcome", "failure"),
			slog.String("reason", "invalid_credentials"),
			slog.String("user_id", user.ID),
		)
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid credentials"})
		return
	}

	// Check TOTP 2FA if enabled
	totpValid, totpErr := checkTOTP(c.Request.Context(), user.ID, req.TOTPCode)
	if totpErr != nil {
		respondInternalError(c, totpErr, "verify 2FA")
		return
	}
	if !totpValid {
		totpReason := "totp_invalid"
		if strings.TrimSpace(req.TOTPCode) == "" {
			totpReason = "totp_required"
		}
		slog.WarnContext(c.Request.Context(), "auth_login_failed",
			slog.String("event", "auth.login"),
			slog.String("outcome", "failure"),
			slog.String("reason", totpReason),
			slog.String("user_id", user.ID),
		)
		c.JSON(http.StatusUnauthorized, gin.H{
			"error":         "TOTP code required",
			"totp_required": true,
		})
		return
	}

	recordSuccessfulLogin(c.Request.Context(), req.Email)

	// Access token: 30 minutes
	accessClaims := CustomClaims{
		TenantID: user.TenantID,
		UserID:   user.ID,
		Email:    user.Email,
		Role:     user.Role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(30 * time.Minute)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
			Subject:   "access",
		},
	}

	accessToken := jwt.NewWithClaims(jwt.SigningMethodHS256, accessClaims)
	accessTokenString, err := accessToken.SignedString([]byte(appConfig.JWTSecret))
	if err != nil {
		respondInternalError(c, err, "generate access token")
		return
	}

	// Refresh token: 7 days
	refreshClaims := CustomClaims{
		TenantID: user.TenantID,
		UserID:   user.ID,
		Email:    user.Email,
		Role:     user.Role,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(7 * 24 * time.Hour)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
			Subject:   "refresh",
		},
	}

	refreshToken := jwt.NewWithClaims(jwt.SigningMethodHS256, refreshClaims)
	refreshTokenString, err := refreshToken.SignedString([]byte(appConfig.JWTSecret))
	if err != nil {
		respondInternalError(c, err, "generate refresh token")
		return
	}

	// Set refresh token as httpOnly cookie
	// FIX #10: Secure: true unconditionally — behind TLS-terminating proxy c.Request.TLS is always nil
	secureCookie := getEnvOrDefault("ZOVARK_COOKIE_SECURE", "true") != "false"
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     "refresh_token",
		Value:    refreshTokenString,
		HttpOnly: true,
		Secure:   secureCookie,
		SameSite: http.SameSiteStrictMode,
		MaxAge:   7 * 24 * 60 * 60,
		Path:     "/",
	})

	slog.InfoContext(c.Request.Context(), "auth_login",
		slog.String("event", "auth.login"),
		slog.String("outcome", "success"),
		slog.String("user_id", user.ID),
		slog.String("tenant_id", user.TenantID),
		slog.String("role", user.Role),
	)

	c.JSON(http.StatusOK, gin.H{
		"token": accessTokenString,
		"user": map[string]interface{}{
			"id":        user.ID,
			"email":     user.Email,
			"role":      user.Role,
			"tenant_id": user.TenantID,
		},
	})
}

// refreshHandler issues a new access token from a valid refresh token cookie.
// POST /api/v1/auth/refresh
func refreshHandler(c *gin.Context) {
	cookie, err := c.Cookie("refresh_token")
	if err != nil || cookie == "" {
		slog.WarnContext(c.Request.Context(), "auth_refresh_failed",
			slog.String("event", "auth.refresh"),
			slog.String("outcome", "failure"),
			slog.String("reason", "missing_refresh_cookie"),
		)
		c.JSON(http.StatusUnauthorized, gin.H{"error": "no refresh token"})
		return
	}

	token, err := jwt.ParseWithClaims(cookie, &CustomClaims{}, func(token *jwt.Token) (interface{}, error) {
		if _, ok := token.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method")
		}
		return []byte(appConfig.JWTSecret), nil
	})
	if err != nil || !token.Valid {
		slog.WarnContext(c.Request.Context(), "auth_refresh_failed",
			slog.String("event", "auth.refresh"),
			slog.String("outcome", "failure"),
			slog.String("reason", "invalid_refresh_token"),
		)
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid refresh token"})
		return
	}

	claims, ok := token.Claims.(*CustomClaims)
	if !ok || claims.Subject != "refresh" {
		slog.WarnContext(c.Request.Context(), "auth_refresh_failed",
			slog.String("event", "auth.refresh"),
			slog.String("outcome", "failure"),
			slog.String("reason", "invalid_refresh_token_type"),
		)
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid refresh token type"})
		return
	}

	// FIX #2: verify user still exists and is active before issuing new token
	var isActive bool
	var currentRole string
	err = dbPool.QueryRow(c.Request.Context(),
		"SELECT is_active, role FROM users WHERE id = $1 AND tenant_id = $2",
		claims.UserID, claims.TenantID,
	).Scan(&isActive, &currentRole)
	if err != nil || !isActive {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "user not found or inactive"})
		return
	}

	// Issue new access token (30 min) using current role from DB
	accessClaims := CustomClaims{
		TenantID: claims.TenantID,
		UserID:   claims.UserID,
		Email:    claims.Email,
		Role:     currentRole,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(30 * time.Minute)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
			Subject:   "access",
		},
	}

	accessToken := jwt.NewWithClaims(jwt.SigningMethodHS256, accessClaims)
	accessTokenString, err := accessToken.SignedString([]byte(appConfig.JWTSecret))
	if err != nil {
		respondInternalError(c, err, "generate access token on refresh")
		return
	}

	slog.InfoContext(c.Request.Context(), "auth_refresh",
		slog.String("event", "auth.refresh"),
		slog.String("outcome", "success"),
		slog.String("user_id", claims.UserID),
		slog.String("tenant_id", claims.TenantID),
	)

	c.JSON(http.StatusOK, gin.H{
		"token": accessTokenString,
		"user": map[string]interface{}{
			"id":        claims.UserID,
			"email":     claims.Email,
			"role":      claims.Role,
			"tenant_id": claims.TenantID,
		},
	})
}

// logoutHandler clears the refresh token cookie.
// POST /api/v1/auth/logout
func logoutHandler(c *gin.Context) {
	slog.InfoContext(c.Request.Context(), "auth_logout",
		slog.String("event", "auth.logout"),
		slog.String("outcome", "success"),
	)
	// FIX #10: Secure: true unconditionally
	secureCookie := getEnvOrDefault("ZOVARK_COOKIE_SECURE", "true") != "false"
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     "refresh_token",
		Value:    "",
		HttpOnly: true,
		Secure:   secureCookie,
		SameSite: http.SameSiteStrictMode,
		MaxAge:   -1,
		Path:     "/",
	})
	c.JSON(http.StatusOK, gin.H{"status": "logged out"})
}
