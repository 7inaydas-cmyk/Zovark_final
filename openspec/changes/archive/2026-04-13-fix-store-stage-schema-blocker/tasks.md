## 1. Pre-flight evidence capture

- [x] 1.1 Snapshot current ledger state for the two target rows: `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT * FROM schema_migrations WHERE filename IN ('040_human_review_flags.sql','046_llm_audit_log.sql')" > openspec/changes/fix-store-stage-schema-blocker/pre_state.txt`
- [x] 1.2 Snapshot current `agent_tasks` columns: `docker exec zovark-postgres psql -U zovark -d zovark -c "\d agent_tasks" >> openspec/changes/fix-store-stage-schema-blocker/pre_state.txt`
- [x] 1.3 Snapshot current `llm_audit_log` existence: `docker exec zovark-postgres psql -U zovark -d zovark -tAc "SELECT to_regclass('public.llm_audit_log')" >> openspec/changes/fix-store-stage-schema-blocker/pre_state.txt`
- [x] 1.4 Capture worker logs proving the live failure: `docker logs --tail 50 zovark_mine-worker-1 2>&1 | grep -A1 "Store failed" > openspec/changes/fix-store-stage-schema-blocker/pre_state_worker.log`

## 2. Confirm the runner cannot fix this on its own

- [x] 2.1 Run `scripts/apply_migrations.sh --dry-run` and capture output to `openspec/changes/fix-store-stage-schema-blocker/dry_run.txt`
- [x] 2.2 Verify dry-run output does NOT include `040_human_review_flags.sql` or `046_llm_audit_log.sql` (proves the lying ledger row is hiding them)
- [x] 2.3 If the dry-run does include either file, STOP — the situation is different from what the proposal assumes; re-investigate before continuing

## 3. Apply 040 + 046 + repair ledger in one transaction

- [x] 3.1 Compose the repair SQL file at `openspec/changes/fix-store-stage-schema-blocker/repair.sql` with the BEGIN/COMMIT block from design.md §Migration Plan step 3 (verbatim DDL from `migrations/040_human_review_flags.sql` and `migrations/046_llm_audit_log.sql`, plus the two `UPDATE schema_migrations` statements)
- [x] 3.2 Apply the repair: `docker exec -i zovark-postgres psql -U zovark -d zovark < openspec/changes/fix-store-stage-schema-blocker/repair.sql 2>&1 | tee openspec/changes/fix-store-stage-schema-blocker/repair.out`
- [x] 3.3 Confirm `repair.out` ends with `COMMIT` and no `ERROR:` lines

## 4. Verify schema is now correct

- [x] 4.1 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT needs_human_review, review_reason FROM agent_tasks LIMIT 1"` — must return without error (zero or one row both fine)
- [x] 4.2 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT 1 FROM llm_audit_log LIMIT 0"` — must return without error
- [x] 4.3 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT filename, source, applied_at FROM schema_migrations WHERE filename IN ('040_human_review_flags.sql','046_llm_audit_log.sql')"` — both rows must show `source='manual_backfill'` and a fresh `applied_at`
- [x] 4.4 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT indexname FROM pg_indexes WHERE tablename IN ('agent_tasks','llm_audit_log') AND indexname IN ('idx_tasks_human_review','idx_llm_audit_task','idx_llm_audit_tenant','idx_llm_audit_created','idx_llm_audit_model')"` — must return all 5 index names

## 5. Verify pipeline end-to-end

- [x] 5.1 Get a fresh JWT: `TOKEN=$(curl -s -X POST http://127.0.0.1:8090/api/v1/auth/login -H "Content-Type: application/json" -d '{"email":"admin@test.local","password":"TestPass2026"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')`
- [x] 5.2 Submit a synthetic brute_force task using the CLAUDE.md curl recipe and capture the returned task_id — task_id `b184a51d-c79b-494b-9be3-30d4223cc0f4`
- [x] 5.3 Poll the task every 2 seconds for up to 60 seconds: `for i in $(seq 1 30); do sleep 2; curl -s http://127.0.0.1:8090/api/v1/tasks/<TASK_ID> -H "Authorization: Bearer $TOKEN" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("status"))'; done`
- [ ] 5.4 **GATE FAILED** — task stayed `pending` for the full 60s window. Worker log shows new error: `Store failed: column "model_name" of relation "agent_tasks" does not exist`. Evidence: `section5_failure.log`. Halting per spec; not in scope to fix the new blocker.
- [ ] 5.5 Skipped — gate 5.4 failed
- [ ] 5.6 Skipped — gate 5.4 failed
- [ ] 5.7 Skipped — gate 5.4 failed

## 6. Capture post-state evidence

- [ ] 6.1 Snapshot post-repair ledger state: append to `openspec/changes/fix-store-stage-schema-blocker/post_state.txt`
- [ ] 6.2 Snapshot post-repair `\d agent_tasks` and `\d llm_audit_log` to `post_state.txt`
- [ ] 6.3 Capture clean worker logs proving Store now succeeds: `docker logs --since 10m zovark_mine-worker-1 2>&1 | grep -E "Started workflow|completed" | tail -20 > post_state_worker.log`
- [ ] 6.4 Save the verifying task_id and its final JSON to `verify_task.json`

## 7. Document follow-up work

- [ ] 7.1 Add a one-line entry to `docs/RUNBOOK_HEALTHCHECK.md#schema-drift` linking this change as the canonical example of a manual ledger repair
- [ ] 7.2 File a follow-up issue (do NOT create as part of this change) titled "Audit remaining init_sql-marked migrations for ledger drift" listing the ~13 other suspect rows
- [ ] 7.3 File a separate follow-up issue titled "Investigate token_quotas.monthly_tokens_used caller mismatch — column name is tokens_used"

## 8. Rollback plan (only if Section 5 verification fails)

- [x] 8.1 If the verification submission does not complete, capture the new `Store failed` log line and any other Stage 5 errors — captured in `section5_failure.log`
- [x] 8.2 Do NOT drop the new columns or table — they are additive and idempotent — confirmed not dropped; Section 4 evidence still valid
- [ ] 8.3 Optionally restore the false `init_sql` ledger rows: `UPDATE schema_migrations SET source='init_sql', applied_at='2026-01-01', applied_by='zovark' WHERE filename IN ('040_human_review_flags.sql','046_llm_audit_log.sql')` — only do this if the failure proves a worse drift exists and we need to keep the symptom visible while we investigate — **NOT executed**: the 040/046 repair is genuinely correct; the new `model_name` error is a separate unrelated drift and reverting would re-mask the work this change fixed. Awaiting user direction.
- [ ] 8.4 Revert the change to `pending` status in OpenSpec and reopen design with the new failure mode documented — **deferred**: pending user decision on whether to (a) widen this change's scope, (b) close as partial-success and open a follow-up change, or (c) revert outright
