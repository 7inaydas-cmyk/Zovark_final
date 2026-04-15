package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

// ============================================================
// LAYER 2: PRE-TEMPORAL BATCH BUFFER
//
// Groups similar alerts by (task_type, source_ip) within a short
// time window. First alert creates the workflow; subsequent alerts
// within the window are absorbed (no workflow created).
//
// Uses Redis atomic operations (Lua script) to prevent race conditions.
// Fail-open: if Redis is unavailable, create workflow normally.
// ============================================================

var (
	apiBatchEnabled       = true
	apiBatchWindowSeconds = 5.0
	apiBatchMaxSize       = 500
)

func init() {
	if v := os.Getenv("ZOVARK_API_BATCH_ENABLED"); v == "false" {
		apiBatchEnabled = false
	}
	if v := os.Getenv("ZOVARK_API_BATCH_WINDOW_SECONDS"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			apiBatchWindowSeconds = f
		}
	}
	if v := os.Getenv("ZOVARK_API_BATCH_MAX_SIZE"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			apiBatchMaxSize = n
		}
	}
}

// Severity-based window multipliers (must match Python smart_batcher.py)
var severityMultiplier = map[string]float64{
	"critical": 0.25,
	"high":     0.5,
	"medium":   1.0,
	"low":      2.0,
	"info":     3.0,
}

// Lua script for atomic batch check-and-increment with severity promotion.
//
// Audit 1.22: src and dst batch keys are now checked in a SINGLE atomic Lua
// invocation so the script observes a consistent view of both keys. The old
// two-step code path (one Eval per key) could race: an alert registered itself
// as the src-key parent, then dst-key said "absorbed into existing batch" —
// leaving an orphaned src-key entry pointing at a task that never runs.
//
// KEYS:
//   KEYS[1] = src key
//   KEYS[2] = dst key (may be the literal empty string if dest_ip is missing)
// ARGV:
//   ARGV[1] = task_id
//   ARGV[2] = now_ts (seconds as float)
//   ARGV[3] = window (seconds as float; severity-adjusted)
//   ARGV[4] = max_size
//   ARGV[5] = ttl (seconds)
//   ARGV[6] = severity (lowercased)
//
// Return: {status, parent_task_id, count}
//   status 0  = first alert across both keys, caller should run the workflow
//   status 1  = absorbed into an existing batch on src OR dst, skip workflow
//   status 2  = window expired / size hit on both keys, started fresh, run workflow
//
// Window expiry uses the ORIGINAL batch window (stored in 'window' field), not
// the current alert's severity-adjusted window. This ensures a critical alert
// arriving within the original batch window gets promoted rather than starting
// a new batch.
const batchLuaScript = `
local src_key = KEYS[1]
local dst_key = KEYS[2]
local task_id = ARGV[1]
local now_ts = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local max_size = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local severity = ARGV[6]

local sev_rank = {critical=5, high=4, medium=3, low=2, info=1}
local new_rank = sev_rank[severity] or 0

-- Helper: inspect a key and return a tuple describing its state.
-- states: "missing", "stale" (expired/overfull), "live".
local function inspect(key)
    if key == nil or key == "" then
        return "missing", nil, nil, nil
    end
    if redis.call('EXISTS', key) == 0 then
        return "missing", nil, nil, nil
    end
    local first_ts = tonumber(redis.call('HGET', key, 'first_ts'))
    local count = tonumber(redis.call('HGET', key, 'count'))
    local batch_task_id = redis.call('HGET', key, 'task_id')
    local batch_severity = redis.call('HGET', key, 'severity') or 'info'
    local batch_window = tonumber(redis.call('HGET', key, 'window') or tostring(window))
    if (now_ts - first_ts) > batch_window or count >= max_size then
        return "stale", batch_task_id, count, batch_severity
    end
    return "live", batch_task_id, count, batch_severity
end

-- Helper: write a fresh batch to a key (no-op for empty-string keys).
local function start(key)
    if key == nil or key == "" then return end
    redis.call('DEL', key)
    redis.call('HSET', key,
        'task_id', task_id,
        'count', 1,
        'first_ts', tostring(now_ts),
        'severity', severity,
        'window', tostring(window))
    redis.call('EXPIRE', key, ttl)
end

-- Helper: absorb into an existing batch and return the (possibly promoted) parent.
local function absorb(key)
    local new_count = redis.call('HINCRBY', key, 'count', 1)
    local cur_severity = redis.call('HGET', key, 'severity') or 'info'
    local cur_rank = sev_rank[cur_severity] or 0
    local parent = redis.call('HGET', key, 'task_id')
    if new_rank > cur_rank then
        redis.call('HSET', key, 'task_id', task_id, 'severity', severity)
        parent = task_id
    end
    return parent, new_count
end

local src_state, src_parent, src_count, _ = inspect(src_key)
local dst_state, dst_parent, dst_count, _ = inspect(dst_key)

-- If either key has a live batch, absorb into it. Prefer src (outbound) when both exist.
if src_state == "live" then
    local parent, count = absorb(src_key)
    -- If dst also live, bump its counter too so future alerts see consistent state.
    if dst_state == "live" then
        absorb(dst_key)
    end
    return {1, parent, count}
end
if dst_state == "live" then
    local parent, count = absorb(dst_key)
    return {1, parent, count}
end

-- No live batch on either key. If any were stale, status=2; otherwise status=0.
local status = 0
if src_state == "stale" or dst_state == "stale" then
    status = 2
end

start(src_key)
start(dst_key)
return {status, task_id, 1}
`

// computeBatchKey creates a grouping key from task_type and source_ip.
func computeBatchKey(taskType, sourceIP string) string {
	raw := strings.ToLower(strings.TrimSpace(taskType)) + ":" + strings.TrimSpace(sourceIP)
	hash := sha256.Sum256([]byte(raw))
	return fmt.Sprintf("%x", hash)[:16]
}

// effectiveBatchWindow returns the batch window in seconds, adjusted for severity.
func effectiveBatchWindow(severity string) float64 {
	mult, ok := severityMultiplier[strings.ToLower(severity)]
	if !ok {
		mult = 1.0
	}
	return apiBatchWindowSeconds * mult
}

// tryBatchAlert checks if this alert should be absorbed into an existing batch.
// Returns (shouldSkip, batchParentTaskID).
// shouldSkip=true means another workflow already covers this alert — don't create a new one.
//
// Audit 1.22: src and dst checks are now one atomic Lua invocation so the
// src→dst transition can't leave an orphaned parent pointing at a task that
// won't run.
//
// Fail-open: returns (false, "") if Redis is unavailable — this path is
// deliberately kept permissive because batching is an optimisation, not a
// correctness gate (backpressure + dedup handle the overload case).
func tryBatchAlert(ctx context.Context, taskType, sourceIP, destIP, severity, taskID string) (bool, string) {
	if !apiBatchEnabled || redisClient == nil {
		return false, ""
	}

	window := effectiveBatchWindow(severity)
	nowTS := float64(time.Now().UnixMilli()) / 1000.0
	ttl := int(window) + 30

	srcKey := "apibatch:src:" + computeBatchKey(taskType, sourceIP)
	dstKey := ""
	if destIP != "" {
		dstKey = "apibatch:dst:" + computeBatchKey(taskType, destIP)
	}

	// Redis-go requires every KEYS[i] to be a non-empty string with a prefix
	// known to the cluster. When dst is absent we pass a sentinel key that the
	// Lua script treats as "missing".
	keys := []string{srcKey, dstKey}
	if dstKey == "" {
		// Use a dummy namespace the script's `inspect` will short-circuit on.
		keys[1] = "apibatch:dst:__none__"
	}

	result, err := redisClient.Eval(ctx, batchLuaScript, keys,
		taskID,
		fmt.Sprintf("%.3f", nowTS),
		fmt.Sprintf("%.3f", window),
		apiBatchMaxSize,
		ttl,
		strings.ToLower(severity),
	).Result()
	if err != nil {
		log.Printf("[BATCH] Redis batch check failed: %v", err)
		return false, ""
	}

	resultSlice, ok := result.([]interface{})
	if !ok || len(resultSlice) < 2 {
		return false, ""
	}
	statusCode, _ := resultSlice[0].(int64)
	batchParentID, _ := resultSlice[1].(string)

	if statusCode == 1 {
		log.Printf("[BATCH] absorbed: type=%s src=%s dst=%s parent=%s",
			taskType, sourceIP, destIP, batchParentID)
		return true, batchParentID
	}
	return false, ""
}
