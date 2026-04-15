## 1. Pre-flight evidence

- [x] 1.1 Snapshot the current `worker/stages/store.py` shape: `wc -l worker/stages/store.py > openspec/changes/fix-store-savepoint-transaction-safety/pre_state.txt && grep -n "def _save_pattern\|def _create_investigation\|def _insert_audit_event\|def _update_dedup_entry\|def _update_task_status\|def _db_conn\|def _savepoint\|cur\.execute\|except Exception" worker/stages/store.py >> openspec/changes/fix-store-savepoint-transaction-safety/pre_state.txt`
- [x] 1.2 Capture a fresh worker log snippet showing the current poisoned-transaction failure mode: `docker logs --since 30m zovark_mine-worker-1 2>&1 | grep -E "Pattern save failed|transaction is aborted|Store failed" | tail -20 > openspec/changes/fix-store-savepoint-transaction-safety/pre_state_worker.log`

## 2. Add the `_savepoint` context manager

- [x] 2.1 In `worker/stages/store.py`, immediately after the existing `_db_conn` context manager (around line 130), add a new `_savepoint(conn, name)` context manager with the body specified in `design.md §Decision 1` (uses `@contextmanager` from the existing `from contextlib import contextmanager` import)
- [x] 2.2 Confirm `_savepoint` issues `SAVEPOINT <name>` on entry, `RELEASE SAVEPOINT <name>` on success, and `ROLLBACK TO SAVEPOINT <name>` on exception, then re-raises
- [x] 2.3 Confirm both the RELEASE and ROLLBACK arms have inner `try/except` to swallow secondary errors (per design.md §Decision 5 and §Risks)

## 3. Wrap `_save_pattern`'s INSERT

- [x] 3.1 In `_save_pattern` (around line 203), wrap the cursor block (the `with conn.cursor() as cur:` containing the `INSERT INTO investigation_memory`) in `with _savepoint(conn, "sp_save_pattern"):`
- [x] 3.2 Do NOT change the INSERT column list. Do NOT change the column names. Do NOT change the table name.
- [x] 3.3 Confirm the surrounding `try: ... except Exception as e: print(f"Pattern save failed (non-fatal): {e}")` is unchanged

## 4. Wrap `_create_investigation`'s INSERT

- [x] 4.1 In `_create_investigation` (around line 222), wrap the cursor block (the `with conn.cursor() as cur:` containing `SET LOCAL synchronous_commit = on;` and the `INSERT INTO investigations RETURNING id`) in `with _savepoint(conn, "sp_create_investigation"):`
- [x] 4.2 Confirm the surrounding `try: ... except Exception as e: print(f"Investigation insert failed (non-fatal): {e}")` is unchanged
- [x] 4.3 Confirm `RETURNING id` still flows through to the `row = cur.fetchone()` line and the function still returns the investigation_id on success

## 5. Wrap `_insert_audit_event`'s INSERT

- [x] 5.1 In `_insert_audit_event` (around line 248), wrap the cursor block in `with _savepoint(conn, "sp_insert_audit_event"):`
- [x] 5.2 Confirm the surrounding `try: ... except Exception as e: print(f"Audit event insert failed (non-fatal): {e}")` is unchanged
- [x] 5.3 Confirm both call sites in `_store_investigation_body` (the `investigation_started` and `investigation_completed` audits) reuse the same savepoint name without modification

## 6. Wrap `_update_dedup_entry`'s inner SELECT

- [x] 6.1 In `_update_dedup_entry` (around line 40), wrap ONLY the inner cursor block (the `with conn.cursor() as cur:` containing `SELECT dedup_hash FROM agent_tasks WHERE id = %s`) in `with _savepoint(conn, "sp_update_dedup_entry"):`
- [x] 6.2 The inner `try: ... except Exception: pass` (around line 48-58) MUST remain in place and must surround the `with _savepoint(...)` so the savepoint helper's re-raised exception is still swallowed
- [x] 6.3 The outer `try: ... except Exception as e: print(f"Dedup entry update failed (non-fatal): {e}")` is unchanged

## 7. Wrap the inline NOTIFY block

- [x] 7.1 In `_store_investigation_body` (around line 416-428), wrap the cursor block inside the `if status == "completed" and tenant_id:` branch (the `with conn.cursor() as cur:` containing `cur.execute("NOTIFY task_completed, %s", ...)`) in `with _savepoint(conn, "sp_notify_task_completed"):`
- [x] 7.2 Confirm the surrounding `try: ... except Exception as notify_err: print(f"NOTIFY failed (non-fatal): {notify_err}")` is unchanged

## 8. Verify NO other functions were touched

- [x] 8.1 Confirm `_update_task_status` (around line 151) is unchanged — no savepoint wrapping, no other modifications — verified at line 176, body intact
- [x] 8.2 Confirm the `SET LOCAL app.current_tenant` block (around line 336-343) is unchanged — verified at line 367
- [x] 8.3 Confirm no file other than `worker/stages/store.py` is touched: `git status --short worker/stages/store.py && git diff --name-only` — only store.py modified in this session; other M files in git status were pre-existing per conversation-start snapshot
- [x] 8.4 Confirm no migration files are added: `git status --short migrations/` — no new migration files created in this change
- [x] 8.5 Run a syntax check: `python3 -c 'import ast; ast.parse(open("worker/stages/store.py").read())'` — OK

## 9. Rebuild and restart the worker

- [x] 9.1 Rebuild the worker image: `docker compose build worker 2>&1 | tail -20`
- [x] 9.2 Restart the worker container: `docker compose up -d worker 2>&1`
- [x] 9.3 Wait for the worker to report healthy: `docker ps --format '{{.Names}}\t{{.Status}}' | grep zovark_mine-worker`
- [x] 9.4 Confirm the worker logs show `Worker starting` with the correct task queue: `docker logs --since 30s zovark_mine-worker-1 2>&1 | grep "Worker starting"`

## 10. Pipeline verification gate

- [x] 10.1 Get a fresh JWT: `TOKEN=$(curl -s -X POST http://127.0.0.1:8090/api/v1/auth/login -H "Content-Type: application/json" -d '{"email":"admin@test.local","password":"TestPass2026"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')`
- [x] 10.2 Submit a synthetic `brute_force` task using the standard CLAUDE.md curl recipe and capture the returned `task_id` — task_id `4ac6136f-3cd6-42ad-9c22-7282364ab029`
- [x] 10.3 Poll the task every 2 seconds for up to 60 seconds — completed on first poll (~2s)
- [x] 10.4 **GATE A** ✓ — task transitioned to `status='completed'`
- [x] 10.5 **GATE B** ✓ — `transaction is aborted` grep returned no matches
- [x] 10.6 **GATE C** ✓ — `Store failed` grep returned no matches
- [x] 10.7 **GATE D** ✓ — `Pattern save failed (non-fatal): column "task_type" of relation "investigation_memory" does not exist` IS still present (fork still trips, no longer fatal)
- [x] 10.8 **GATE E** ✓ — DB row: `status=completed, verdict=true_positive, risk=95, model_name=unknown`

## 11. Post-state evidence

- [x] 11.1 Snapshot the post-fix `git diff worker/stages/store.py` to `openspec/changes/fix-store-savepoint-transaction-safety/post_state_diff.txt`
- [x] 11.2 Capture clean worker logs proving Store now completes for the verifying task: `docker logs --since 10m zovark_mine-worker-1 2>&1 | grep -E "Started workflow|task_completed|Pattern save failed|transaction is aborted|Store failed" | tail -30 > openspec/changes/fix-store-savepoint-transaction-safety/post_state_worker.log`
- [x] 11.3 Save the verifying task_id and its final JSON to `verify_task.json`
- [x] 11.4 Snapshot the verifying task's final agent_tasks row: `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT id, status, model_name, output->>'verdict', output->>'risk_score', completed_at FROM agent_tasks WHERE id='<TASK_ID>'" > openspec/changes/fix-store-savepoint-transaction-safety/post_state_db.txt`

## 12. Document follow-up work (do NOT create as part of this change)

- [x] 12.1 Note: the `investigation_memory` schema fork remains and now produces only a non-fatal warning; reconciliation is a separate code-vs-DB decision change. Already documented in `proposal.md §Out of scope` and `design.md §Open Questions`.
- [x] 12.2 Note: other pipeline stages (`assess.py`, `analyze.py`, `execute.py`, `govern.py`, `ingest.py`) should be audited for the same Python-vs-Postgres `try/except` mismatch. Already documented in `design.md §Open Questions`.
- [x] 12.3 Note: `_savepoint` could later be moved to a shared `worker/db_helpers.py` so other stages can reuse the primitive. Already documented in `design.md §Open Questions`.

## 13. Rollback plan (only if §10 verification fails)

- [ ] 13.1 If any §10 gate fails, capture the new failure mode log to `openspec/changes/fix-store-savepoint-transaction-safety/section10_failure.log`
- [ ] 13.2 Revert the diff: `git checkout worker/stages/store.py`
- [ ] 13.3 Rebuild and restart the worker: `docker compose build worker && docker compose up -d worker`
- [ ] 13.4 Confirm the previous (broken) behavior returns: a fresh task should once again hit `Pattern save failed (non-fatal)` followed by `current transaction is aborted` lines — proving the rollback is clean
- [ ] 13.5 Pause and report the failure mode for user decision
