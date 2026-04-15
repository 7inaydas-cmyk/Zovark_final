package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
)

// ============================================================
// SERVER-SENT EVENTS FOR REAL-TIME UPDATES (Issue #19)
// ============================================================

// sseMaxSubscribers caps concurrent SSE clients holding a pgxpool connection
// for LISTEN. Default is min(pool/3, 200); overridable via env. See task 1.8.
var sseMaxSubscribers int64 = 200

// sseActiveSubscribers is the current count of active LISTEN-holding SSE clients.
var sseActiveSubscribers int64

// firstN returns s[:n] if len(s) >= n, else s. Bounds-safe substring.
// Used to avoid panics on short UUIDs in log/message fields.
func firstN(s string, n int) string {
	if n < 0 || len(s) <= n {
		return s
	}
	return s[:n]
}

// sseWriteJSONEvent emits a well-formed SSE event frame with JSON-marshalled data.
// Returns false if the write to the client failed (slow/disconnected consumer).
func sseWriteJSONEvent(c *gin.Context, event string, payload interface{}) bool {
	data, err := json.Marshal(payload)
	if err != nil {
		return false
	}
	if event != "" {
		if _, werr := c.Writer.WriteString("event: " + event + "\n"); werr != nil {
			return false
		}
	}
	if _, werr := c.Writer.WriteString("data: " + string(data) + "\n\n"); werr != nil {
		return false
	}
	c.Writer.Flush()
	return true
}

// taskSSEHandler streams task status updates via Server-Sent Events.
// GET /api/v1/tasks/:id/stream
func taskSSEHandler(c *gin.Context) {
	taskID := c.Param("id")
	tenantID := c.MustGet("tenant_id").(string)

	// Verify task exists and belongs to tenant
	var exists bool
	err := dbPool.QueryRow(c.Request.Context(),
		"SELECT EXISTS(SELECT 1 FROM agent_tasks WHERE id = $1 AND tenant_id = $2)", taskID, tenantID,
	).Scan(&exists)
	if err != nil || !exists {
		c.JSON(http.StatusNotFound, gin.H{"error": "task not found"})
		return
	}

	// Set SSE headers
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("Transfer-Encoding", "chunked")
	c.Header("X-Accel-Buffering", "no") // Disable nginx buffering

	// Track previous state
	var lastStatus string
	var lastStepCount int

	// Send initial connection event (JSON-encoded to prevent injection via task_id).
	sseWriteJSONEvent(c, "connected", map[string]string{
		"task_id": taskID,
		"message": "SSE stream connected",
	})

	// Poll every 2 seconds
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	// Listen for client disconnect
	ctx := c.Request.Context()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			// Query current task status
			var status string
			var completedAt *time.Time
			var executionMs *int
			var output *string

			err := dbPool.QueryRow(ctx,
				`SELECT status, completed_at, execution_ms, output::text
				 FROM agent_tasks WHERE id = $1 AND tenant_id = $2`,
				taskID, tenantID,
			).Scan(&status, &completedAt, &executionMs, &output)
			if err != nil {
				log.Printf("SSE: error querying task %s: %v", taskID, err)
				continue
			}

			// Check for status change
			if status != lastStatus {
				if !sseWriteJSONEvent(c, "status_changed", map[string]string{
					"task_id":         taskID,
					"status":          status,
					"previous_status": lastStatus,
				}) {
					return
				}
				lastStatus = status
			}

			// Check for new steps
			var stepCount int
			_ = dbPool.QueryRow(ctx,
				"SELECT COUNT(*) FROM investigation_steps WHERE task_id = $1 AND tenant_id = $2", taskID, tenantID,
			).Scan(&stepCount)

			if stepCount > lastStepCount {
				// Get the latest step info
				var stepNum int
				var stepType, stepStatus string
				var stepOutput *string
				_ = dbPool.QueryRow(ctx,
					`SELECT step_number, step_type, status, output
					 FROM investigation_steps WHERE task_id = $1 AND tenant_id = $2
					 ORDER BY step_number DESC LIMIT 1`, taskID, tenantID,
				).Scan(&stepNum, &stepType, &stepStatus, &stepOutput)

				outputSnippet := ""
				if stepOutput != nil {
					outputSnippet = *stepOutput
					if len(outputSnippet) > 500 {
						outputSnippet = outputSnippet[:500] + "..."
					}
				}
				if !sseWriteJSONEvent(c, "step_completed", map[string]interface{}{
					"task_id":     taskID,
					"step_number": stepNum,
					"step_type":   stepType,
					"status":      stepStatus,
					"output":      outputSnippet,
				}) {
					return
				}
				lastStepCount = stepCount
			}

			// If investigation is complete, send final event and close
			if status == "completed" || status == "failed" || status == "cancelled" {
				ms := 0
				if executionMs != nil {
					ms = *executionMs
				}
				sseWriteJSONEvent(c, "investigation_complete", map[string]interface{}{
					"task_id":      taskID,
					"status":       status,
					"execution_ms": ms,
					"step_count":   stepCount,
				})
				return
			}
		}
	}
}

// streamAllTaskUpdates provides a global SSE stream for all task completions.
// GET /api/v1/tasks/stream
// Supports token as query param since EventSource doesn't support headers.
//
// NOTE(audit 1.8): Each LISTEN connection holds a pgxpool slot for its lifetime
// (PostgreSQL requires the same backend for the entire LISTEN). We cap the
// concurrent subscriber count below the pool size so SSE cannot drain every
// connection and starve normal API queries.
func streamAllTaskUpdates(c *gin.Context) {
	tenantID := c.MustGet("tenant_id").(string)

	// Capacity check: bound concurrent LISTEN holders.
	if atomic.LoadInt64(&sseActiveSubscribers) >= sseMaxSubscribers {
		c.Header("Retry-After", "5")
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"error": "sse capacity exceeded",
			"code":  "sse_capacity_exceeded",
		})
		return
	}
	atomic.AddInt64(&sseActiveSubscribers, 1)
	defer atomic.AddInt64(&sseActiveSubscribers, -1)

	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")

	// Send connected event — bounds-safe tenant prefix via firstN, JSON payload.
	sseWriteJSONEvent(c, "connected", map[string]string{
		"message": "SSE stream connected",
		"tenant":  firstN(tenantID, 8),
	})

	// Try to use PostgreSQL LISTEN/NOTIFY
	conn, err := dbPool.Acquire(c.Request.Context())
	if err != nil {
		log.Printf("SSE: failed to acquire DB connection: %v", err)
		sseWriteJSONEvent(c, "error", map[string]string{"error": "db connection failed"})
		return
	}
	defer conn.Release()

	// LISTEN for task completions (NOTIFY sent by store.py)
	_, err = conn.Exec(c.Request.Context(), "LISTEN task_completed")
	if err != nil {
		log.Printf("SSE: LISTEN failed, falling back to polling: %v", err)
		streamAllTasksPolling(c, tenantID)
		return
	}
	// LISTEN for investigation events (NOTIFY sent by events.py — waterfall streaming)
	_, _ = conn.Exec(c.Request.Context(), "LISTEN investigation_events")

	ctx := c.Request.Context()
	pgxConn := conn.Conn()
	keepalive := 15 * time.Second

	defer func() {
		unsub, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = conn.Exec(unsub, "UNLISTEN task_completed")
		_, _ = conn.Exec(unsub, "UNLISTEN investigation_events")
	}()

	for ctx.Err() == nil {
		waitCtx, cancel := context.WithTimeout(ctx, keepalive)
		notification, err := pgxConn.WaitForNotification(waitCtx)
		cancel()

		if ctx.Err() != nil {
			return
		}

		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) {
				_, werr := c.Writer.WriteString(": keepalive\n\n")
				if werr != nil {
					return
				}
				c.Writer.Flush()
				continue
			}
			log.Printf("SSE: WaitForNotification: %v", err)
			continue
		}

		if notification == nil {
			continue
		}

		var payload map[string]interface{}
		if json.Unmarshal([]byte(notification.Payload), &payload) != nil {
			continue
		}
		if payloadTenant, ok := payload["tenant_id"].(string); ok && payloadTenant != tenantID {
			continue
		}
		data, _ := json.Marshal(payload)
		eventType := "task_completed"
		if et, ok := payload["event_type"].(string); ok && et != "" {
			eventType = et
		}
		if eventType == "task_completed" {
			if tid, ok := payload["task_id"].(string); ok {
				ptid := ""
				if pt, ok := payload["tenant_id"].(string); ok {
					ptid = pt
				}
				triggerPushbackFromNotify(tid, ptid)
			}
		}
		if _, werr := c.Writer.WriteString("event: " + eventType + "\n"); werr != nil {
			return
		}
		if traceID, ok := payload["trace_id"].(string); ok && traceID != "" {
			if _, werr := c.Writer.WriteString("id: " + traceID + "\n"); werr != nil {
				return
			}
		}
		if _, werr := c.Writer.WriteString("data: " + string(data) + "\n\n"); werr != nil {
			return
		}
		c.Writer.Flush()
	}
}

// streamAllTasksPolling is a fallback that polls for recently completed tasks.
func streamAllTasksPolling(c *gin.Context, tenantID string) {
	ctx := c.Request.Context()
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()

	lastCheck := time.Now()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			rows, err := dbPool.Query(ctx, `
				SELECT id, task_type, status, (output->>'verdict')::text, (output->>'risk_score')::int
				FROM agent_tasks
				WHERE tenant_id = $1 AND completed_at > $2
				ORDER BY completed_at DESC LIMIT 10
			`, tenantID, lastCheck)
			if err != nil {
				continue
			}

			for rows.Next() {
				var id, taskType, status string
				var verdict *string
				var riskScore *int
				if err := rows.Scan(&id, &taskType, &status, &verdict, &riskScore); err != nil {
					continue
				}
				sseWriteJSONEvent(c, "task_completed", map[string]interface{}{
					"task_id":    id,
					"task_type":  taskType,
					"status":     status,
					"verdict":    verdict,
					"risk_score": riskScore,
				})
			}
			rows.Close()
			lastCheck = time.Now()
		}
	}
}

