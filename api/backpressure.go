package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// ============================================================
// LAYER 3: TEMPORAL QUEUE DEPTH BACKPRESSURE
//
// Tracks active workflow count and throttles creation when the
// queue is deep. Uses Redis sorted set for distributed tracking.
//
// Soft limit: queue task in DB (status='queued'), return 202
// Hard limit: reject with 503 + Retry-After
//
// Background drain goroutine processes queued tasks.
// Fail-open: if Redis unavailable, create workflow immediately.
// ============================================================

var (
	backpressureEnabled   = true
	maxPendingWorkflows   = 200
	maxPendingHard        = 1000
	backpressureWindowSec = 120 // Track workflows started in last N seconds
)

func init() {
	if v := os.Getenv("ZOVARK_BACKPRESSURE_ENABLED"); v == "false" {
		backpressureEnabled = false
	}
	if v := os.Getenv("ZOVARK_MAX_PENDING_WORKFLOWS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			maxPendingWorkflows = n
		}
	}
	if v := os.Getenv("ZOVARK_MAX_PENDING_WORKFLOWS_HARD"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			maxPendingHard = n
		}
	}
}

const backpressureKey = "temporal:workflow_starts"

// checkBackpressure returns (allowed, queueDepth).
// allowed=true means a new workflow can be started.
// allowed=false with queueDepth < hard limit means "queue it".
// allowed=false with queueDepth >= hard limit means "reject".
//
// Audit 1.21: FAIL-CLOSED on Redis error. Previously this returned (true, 0)
// on any Redis hiccup, silently disabling backpressure and admitting unlimited
// concurrent workflow starts. Now on Redis error we return (false, soft_limit)
// so the caller routes new tasks into the queued-for-drain path instead of
// flooding Temporal. Per-command errors in the pipeline are also inspected.
func checkBackpressure(ctx context.Context) (bool, int) {
	if !backpressureEnabled || redisClient == nil {
		return true, 0
	}

	now := float64(time.Now().Unix())
	cutoff := now - float64(backpressureWindowSec)

	// Clean old entries and count current.
	pipe := redisClient.Pipeline()
	zremCmd := pipe.ZRemRangeByScore(ctx, backpressureKey, "-inf", fmt.Sprintf("%.0f", cutoff))
	countCmd := pipe.ZCard(ctx, backpressureKey)
	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("[BACKPRESSURE] pipeline exec failed (fail-closed to soft limit): %v", err)
		return false, maxPendingWorkflows
	}
	if err := zremCmd.Err(); err != nil {
		log.Printf("[BACKPRESSURE] ZRemRangeByScore error (fail-closed): %v", err)
		return false, maxPendingWorkflows
	}
	if err := countCmd.Err(); err != nil {
		log.Printf("[BACKPRESSURE] ZCard error (fail-closed): %v", err)
		return false, maxPendingWorkflows
	}

	depth := int(countCmd.Val())

	if depth >= maxPendingHard {
		return false, depth
	}
	if depth >= maxPendingWorkflows {
		return false, depth
	}
	return true, depth
}

// isHardLimitReached returns true if queue depth exceeds the hard limit.
func isHardLimitReached(depth int) bool {
	return depth >= maxPendingHard
}

// recordWorkflowStart tracks a newly started workflow for backpressure counting.
func recordWorkflowStart(ctx context.Context, workflowID string) {
	if !backpressureEnabled || redisClient == nil {
		return
	}

	now := float64(time.Now().Unix())
	err := redisClient.ZAdd(ctx, backpressureKey, redis.Z{Score: now, Member: workflowID}).Err()
	if err != nil {
		log.Printf("[BACKPRESSURE] Failed to record workflow start: %v", err)
	}
	// Set expiry on the sorted set to auto-cleanup
	redisClient.Expire(ctx, backpressureKey, time.Duration(backpressureWindowSec+60)*time.Second)
}

// startQueueDrainLoop runs a background goroutine that processes queued tasks.
// It polls agent_tasks WHERE status='queued' and starts workflows for them.
func startQueueDrainLoop(ctx context.Context) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	log.Println("[BACKPRESSURE] Queue drain goroutine started")

	for {
		select {
		case <-ctx.Done():
			log.Println("[BACKPRESSURE] Queue drain goroutine stopped")
			return
		case <-ticker.C:
			// Compute dynamic drain count based on headroom
			_, depth := checkBackpressure(ctx)
			headroom := maxPendingWorkflows - depth
			if headroom <= 0 {
				continue // At capacity, wait
			}
			drainCount := headroom / 10
			if drainCount < 1 {
				drainCount = 1
			}
			if drainCount > 30 {
				drainCount = 30
			}
			drainQueuedTasks(ctx, drainCount)
		}
	}
}

// drainQueuedTasks processes up to maxDrain queued tasks per tick.
//
// Audit 1.20: this function used to run a plain SELECT + separate UPDATE, so two
// API replicas racing on the drain tick would both see the same queued rows and
// both publish the same task to Redpanda — producing duplicate investigations.
// Now we use a single transaction with SELECT ... FOR UPDATE SKIP LOCKED +
// UPDATE ... RETURNING so Postgres hands out each queued task to exactly one
// replica. Publishes happen after the transaction commits so a Redpanda failure
// is reverted by a cleanup UPDATE on a detached context.
func drainQueuedTasks(ctx context.Context, maxDrain int) {
	if dbPool == nil || tc == nil {
		return
	}

	// Check if we have capacity.
	allowed, _ := checkBackpressure(ctx)
	if !allowed {
		return
	}

	tx, err := dbPool.Begin(ctx)
	if err != nil {
		log.Printf("[DRAIN] begin tx failed: %v", err)
		return
	}
	// defer rollback as a no-op after commit.
	defer func() { _ = tx.Rollback(ctx) }()

	// Claim up to maxDrain queued tasks atomically: FOR UPDATE SKIP LOCKED lets
	// other API replicas claim different rows concurrently without blocking.
	claimRows, err := tx.Query(ctx,
		`WITH claimed AS (
		    SELECT id
		      FROM agent_tasks
		     WHERE status = 'queued'
		     ORDER BY created_at ASC
		     FOR UPDATE SKIP LOCKED
		     LIMIT $1
		)
		UPDATE agent_tasks
		   SET status = 'pending'
		 WHERE id IN (SELECT id FROM claimed)
		RETURNING id, tenant_id, task_type, input`,
		maxDrain)
	if err != nil {
		log.Printf("[DRAIN] claim query failed: %v", err)
		return
	}

	type claimedTask struct {
		ID       string
		TenantID string
		TaskType string
		Input    map[string]interface{}
	}
	var claimed []claimedTask
	for claimRows.Next() {
		var t claimedTask
		if err := claimRows.Scan(&t.ID, &t.TenantID, &t.TaskType, &t.Input); err != nil {
			log.Printf("[DRAIN] scan row failed: %v", err)
			continue
		}
		claimed = append(claimed, t)
	}
	claimRows.Close()

	if err := tx.Commit(ctx); err != nil {
		log.Printf("[DRAIN] tx commit failed (claimed rows abandoned, still queued): %v", err)
		return
	}

	// Post-commit publish. If publish fails we flip the row back to 'failed'
	// via a detached context so a cancelled drain ctx can't swallow the write.
	for _, t := range claimed {
		// Re-check backpressure per task so a burst doesn't overwhelm Temporal.
		if ok, _ := checkBackpressure(ctx); !ok {
			// Roll the unused tasks back to queued so the next tick retries them.
			cleanCtx, cleanCancel := detachedCleanupCtx()
			_, _ = dbPool.Exec(cleanCtx, "UPDATE agent_tasks SET status = 'queued' WHERE id = $1", t.ID)
			cleanCancel()
			continue
		}

		pubCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		err := publishTaskNew(pubCtx, t.TenantID, t.ID, t.TaskType, t.Input)
		cancel()
		if err != nil {
			log.Printf("[DRAIN] failed to publish queued task %s: %v", t.ID, err)
			cleanCtx, cleanCancel := detachedCleanupCtx()
			_, _ = dbPool.Exec(cleanCtx, "UPDATE agent_tasks SET status = 'failed' WHERE id = $1", t.ID)
			cleanCancel()
			continue
		}

		recordWorkflowStart(ctx, "task-"+t.ID)
		log.Printf("[DRAIN] published queued task %s (type=%s) to Redpanda", t.ID, t.TaskType)
	}
}
