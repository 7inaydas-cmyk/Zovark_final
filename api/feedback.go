package main

import (
	"context"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

// feedbackRefreshPending is a monotonic counter bumped by each submitFeedback
// call; a single background goroutine runs at most one REFRESH MATERIALIZED
// VIEW every 30 seconds when any bump arrived in the interval. Replaces the
// per-submit spawn that could flood the pool during feedback bursts.
var feedbackRefreshPending int64

// feedbackRefreshStarted is atomically CAS-flipped on first call to ensure the
// debounce goroutine starts exactly once.
var feedbackRefreshStarted int32

func startFeedbackRefreshDebouncerOnce() {
	if !atomic.CompareAndSwapInt32(&feedbackRefreshStarted, 0, 1) {
		return
	}
	safeGoroutine("feedback_refresh_debouncer", func(_ context.Context) {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			if atomic.SwapInt64(&feedbackRefreshPending, 0) > 0 {
				refreshCtx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
				_, _ = dbPool.Exec(refreshCtx, "REFRESH MATERIALIZED VIEW CONCURRENTLY feedback_accuracy")
				cancel()
			}
		}
	})
}

type FeedbackRequest struct {
	VerdictCorrect    *bool    `json:"verdict_correct"`
	CorrectedVerdict  string   `json:"corrected_verdict"`
	FalsePositive     bool     `json:"false_positive"`
	MissedThreat      bool     `json:"missed_threat"`
	Notes             string   `json:"notes"`
	AnalystConfidence *float64 `json:"analyst_confidence"`
}

func submitFeedbackHandler(c *gin.Context) {
	investigationID := c.Param("id")
	if _, err := uuid.Parse(investigationID); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid investigation ID"})
		return
	}

	var req FeedbackRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	tenantID := c.GetString("tenant_id")
	userID := c.GetString("user_id")

	// Verify the investigation belongs to the caller's tenant
	var ownerTenant string
	err := dbPool.QueryRow(c.Request.Context(), // FIX #11
		"SELECT tenant_id FROM investigations WHERE id = $1", investigationID,
	).Scan(&ownerTenant)
	if err != nil || ownerTenant != tenantID {
		c.JSON(http.StatusNotFound, gin.H{"error": "investigation not found"})
		return
	}

	feedbackID := uuid.New().String()

	_, err = dbPool.Exec(c.Request.Context(), // FIX #11
		`INSERT INTO investigation_feedback
			(id, investigation_id, tenant_id, analyst_id, verdict_correct, corrected_verdict,
			 false_positive, missed_threat, notes, analyst_confidence)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
		feedbackID, investigationID, tenantID, userID,
		req.VerdictCorrect, req.CorrectedVerdict,
		req.FalsePositive, req.MissedThreat, req.Notes, req.AnalystConfidence,
	)
	if err != nil {
		respondInternalError(c, err, "store feedback")
		return
	}

	// Debounced refresh: flag dirty and let the 30-second ticker coalesce bursts.
	// Previously spawned a goroutine per feedback which, under an adversarial
	// burst, serialised REFRESH commands and starved the connection pool.
	atomic.AddInt64(&feedbackRefreshPending, 1)
	startFeedbackRefreshDebouncerOnce()

	c.JSON(http.StatusCreated, gin.H{
		"id":               feedbackID,
		"investigation_id": investigationID,
		"status":           "recorded",
	})
}

func getFeedbackStatsHandler(c *gin.Context) {
	tenantID := c.GetString("tenant_id")

	var stats struct {
		Total          int     `json:"total"`
		Correct        int     `json:"correct"`
		Incorrect      int     `json:"incorrect"`
		FalsePositives int     `json:"false_positives"`
		MissedThreats  int     `json:"missed_threats"`
		AccuracyRate   float64 `json:"accuracy_rate"`
		AvgAnalystConf float64 `json:"avg_analyst_confidence"`
	}

	err := dbPool.QueryRow(c.Request.Context(), // FIX #11
		`SELECT
			COUNT(*)::int,
			COALESCE(SUM(CASE WHEN verdict_correct THEN 1 ELSE 0 END), 0)::int,
			COALESCE(SUM(CASE WHEN NOT verdict_correct THEN 1 ELSE 0 END), 0)::int,
			COALESCE(SUM(CASE WHEN false_positive THEN 1 ELSE 0 END), 0)::int,
			COALESCE(SUM(CASE WHEN missed_threat THEN 1 ELSE 0 END), 0)::int,
			COALESCE(AVG(CASE WHEN verdict_correct THEN 1.0 ELSE 0.0 END), 0)::float8,
			COALESCE(AVG(analyst_confidence), 0)::float8
		 FROM investigation_feedback
		 WHERE tenant_id = $1`, tenantID,
	).Scan(
		&stats.Total, &stats.Correct, &stats.Incorrect,
		&stats.FalsePositives, &stats.MissedThreats,
		&stats.AccuracyRate, &stats.AvgAnalystConf,
	)
	if err != nil {
		respondInternalError(c, err, "fetch feedback stats")
		return
	}

	c.JSON(http.StatusOK, stats)
}
