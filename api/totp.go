package main

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base32"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"time"

	"github.com/gin-gonic/gin"
)

// encryptSecret encrypts a plaintext string using AES-256-GCM (Security P2#21).
func encryptSecret(plaintext string) (string, error) {
	key := []byte(os.Getenv("ZOVARK_ENCRYPTION_KEY"))
	if len(key) != 32 {
		return plaintext, nil // Graceful fallback if key not configured
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", fmt.Errorf("cipher init: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", fmt.Errorf("gcm init: %w", err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", fmt.Errorf("nonce gen: %w", err)
	}
	ct := gcm.Seal(nonce, nonce, []byte(plaintext), nil)
	return "enc:" + base64.StdEncoding.EncodeToString(ct), nil
}

// decryptSecret decrypts an AES-256-GCM encrypted string (Security P2#21).
func decryptSecret(encrypted string) (string, error) {
	// Handle unencrypted legacy values
	if len(encrypted) < 4 || encrypted[:4] != "enc:" {
		return encrypted, nil
	}
	key := []byte(os.Getenv("ZOVARK_ENCRYPTION_KEY"))
	if len(key) != 32 {
		return "", fmt.Errorf("ZOVARK_ENCRYPTION_KEY must be 32 bytes for decryption")
	}
	data, err := base64.StdEncoding.DecodeString(encrypted[4:])
	if err != nil {
		return "", fmt.Errorf("base64 decode: %w", err)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", fmt.Errorf("cipher init: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", fmt.Errorf("gcm init: %w", err)
	}
	ns := gcm.NonceSize()
	if len(data) < ns {
		return "", fmt.Errorf("ciphertext too short")
	}
	pt, err := gcm.Open(nil, data[:ns], data[ns:], nil)
	if err != nil {
		return "", fmt.Errorf("decrypt: %w", err)
	}
	return string(pt), nil
}

// ============================================================
// TWO-FACTOR AUTHENTICATION — TOTP (Issue #4)
// ============================================================

const (
	totpDigits = 6
	totpPeriod = 30 // seconds
	totpIssuer = "ZOVARK"
)

// generateTOTPSecret creates a random 20-byte secret encoded as base32.
func generateTOTPSecret() (string, error) {
	secret := make([]byte, 20)
	_, err := rand.Read(secret)
	if err != nil {
		return "", err
	}
	return base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(secret), nil
}

// generateTOTP computes a TOTP code for the given secret and time.
func generateTOTP(secret string, t time.Time) (string, error) {
	key, err := base32.StdEncoding.WithPadding(base32.NoPadding).DecodeString(secret)
	if err != nil {
		return "", fmt.Errorf("invalid secret: %w", err)
	}

	counter := uint64(t.Unix()) / totpPeriod

	// Convert counter to big-endian bytes
	buf := make([]byte, 8)
	binary.BigEndian.PutUint64(buf, counter)

	// HMAC-SHA1
	mac := hmac.New(sha1.New, key)
	mac.Write(buf)
	hash := mac.Sum(nil)

	// Dynamic truncation
	offset := hash[len(hash)-1] & 0x0f
	code := binary.BigEndian.Uint32(hash[offset:offset+4]) & 0x7fffffff

	// Modulo to get desired number of digits
	modulo := uint32(math.Pow10(totpDigits))
	otp := code % modulo

	return fmt.Sprintf("%0*d", totpDigits, otp), nil
}

// verifyTOTP checks a TOTP code against the secret, allowing a +/- 1 period window.
func verifyTOTP(secret, code string) bool {
	now := time.Now()

	// Check current period and +/- 1 period for clock skew
	for _, offset := range []int{-1, 0, 1} {
		t := now.Add(time.Duration(offset*totpPeriod) * time.Second)
		expected, err := generateTOTP(secret, t)
		if err != nil {
			continue
		}
		if hmac.Equal([]byte(expected), []byte(code)) {
			return true
		}
	}
	return false
}

// totpSetupHandler generates a TOTP secret and returns the provisioning URI.
// POST /api/v1/auth/totp/setup
func totpSetupHandler(c *gin.Context) {
	userID := c.MustGet("user_id").(string)

	// Check if TOTP is already enabled
	var totpEnabled bool
	err := dbPool.QueryRow(c.Request.Context(), // FIX #11
		"SELECT totp_enabled FROM users WHERE id = $1", userID,
	).Scan(&totpEnabled)
	if err != nil {
		respondError(c, http.StatusNotFound, "USER_NOT_FOUND", "User not found")
		return
	}

	if totpEnabled {
		respondError(c, http.StatusConflict, "TOTP_ALREADY_ENABLED", "TOTP is already enabled for this account")
		return
	}

	// Generate secret
	secret, err := generateTOTPSecret()
	if err != nil {
		respondError(c, http.StatusInternalServerError, "SECRET_GENERATION_FAILED", "Failed to generate TOTP secret")
		return
	}

	// Get user email for the provisioning URI
	var email string
	_ = dbPool.QueryRow(c.Request.Context(), // FIX #11
		"SELECT email FROM users WHERE id = $1", userID,
	).Scan(&email)

	// Encrypt and store secret (not yet enabled — needs verification)
	encryptedSecret, encErr := encryptSecret(secret)
	if encErr != nil {
		respondError(c, http.StatusInternalServerError, "ENCRYPT_FAILED", "Failed to encrypt TOTP secret")
		return
	}
	_, err = dbPool.Exec(c.Request.Context(), // FIX #11
		"UPDATE users SET totp_secret = $1 WHERE id = $2",
		encryptedSecret, userID,
	)
	if err != nil {
		respondError(c, http.StatusInternalServerError, "SAVE_FAILED", "Failed to save TOTP secret")
		return
	}

	// Build otpauth:// URI for QR code
	otpauthURI := fmt.Sprintf("otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d",
		url.PathEscape(totpIssuer),
		url.PathEscape(email),
		secret,
		url.QueryEscape(totpIssuer),
		totpDigits,
		totpPeriod,
	)

	respondOK(c, gin.H{
		"secret":       secret,
		"otpauth_uri":  otpauthURI,
		"instructions": "Scan the QR code or enter the secret in your authenticator app, then verify with POST /api/v1/auth/totp/verify",
	})
}

// totpVerifyHandler verifies a TOTP code and enables 2FA for the user.
// POST /api/v1/auth/totp/verify
func totpVerifyHandler(c *gin.Context) {
	userID := c.MustGet("user_id").(string)

	var req struct {
		Code string `json:"code" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		respondError(c, http.StatusBadRequest, "INVALID_REQUEST", err.Error())
		return
	}

	// Get stored secret
	var secret *string
	var totpEnabled bool
	err := dbPool.QueryRow(c.Request.Context(), // FIX #11
		"SELECT totp_secret, totp_enabled FROM users WHERE id = $1", userID,
	).Scan(&secret, &totpEnabled)
	if err != nil || secret == nil {
		respondError(c, http.StatusBadRequest, "NO_SECRET", "No TOTP secret found. Call /auth/totp/setup first.")
		return
	}

	if totpEnabled {
		respondError(c, http.StatusConflict, "TOTP_ALREADY_ENABLED", "TOTP is already enabled")
		return
	}

	// Decrypt the secret before verification
	decrypted, decErr := decryptSecret(*secret)
	if decErr != nil {
		respondError(c, http.StatusInternalServerError, "DECRYPT_FAILED", "Failed to decrypt TOTP secret")
		return
	}

	// Verify the code
	if !verifyTOTP(decrypted, req.Code) {
		respondError(c, http.StatusUnauthorized, "INVALID_CODE", "Invalid TOTP code")
		return
	}

	// Enable TOTP
	_, err = dbPool.Exec(c.Request.Context(), // FIX #11
		"UPDATE users SET totp_enabled = true WHERE id = $1", userID,
	)
	if err != nil {
		respondError(c, http.StatusInternalServerError, "ENABLE_FAILED", "Failed to enable TOTP")
		return
	}

	respondOK(c, gin.H{
		"status":  "enabled",
		"message": "Two-factor authentication is now enabled",
	})
}

// checkTOTP verifies the TOTP code during login when 2FA is enabled.
// Returns true if TOTP is not enabled or code is valid. Accepts a context so
// the request lifetime can cancel the DB query (originally used
// context.Background, which made tracing/cancellation impossible).
func checkTOTP(ctx context.Context, userID, totpCode string) (bool, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	var secret *string
	var totpEnabled bool

	err := dbPool.QueryRow(ctx,
		"SELECT totp_secret, totp_enabled FROM users WHERE id = $1", userID,
	).Scan(&secret, &totpEnabled)
	if err != nil {
		return false, err
	}

	if !totpEnabled {
		return true, nil // TOTP not enabled, allow login
	}

	if totpCode == "" {
		return false, nil // TOTP required but not provided
	}

	if secret == nil {
		return false, fmt.Errorf("TOTP enabled but no secret configured")
	}

	// Decrypt before verification
	decrypted, err := decryptSecret(*secret)
	if err != nil {
		return false, fmt.Errorf("failed to decrypt TOTP secret: %w", err)
	}

	return verifyTOTP(decrypted, totpCode), nil
}
