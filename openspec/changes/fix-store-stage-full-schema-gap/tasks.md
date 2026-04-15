## 1. Pre-flight evidence capture

- [x] 1.1 Snapshot 047 ledger row state: `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT * FROM schema_migrations WHERE filename='047_add_model_name.sql'" > openspec/changes/fix-store-stage-full-schema-gap/pre_state.txt`
- [x] 1.2 Snapshot current `agent_tasks.model_name` and `investigations.model_name` absence: `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT 'agent_tasks.model_name' AS col, EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='agent_tasks' AND column_name='model_name') AS exists UNION ALL SELECT 'investigations.model_name', EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='investigations' AND column_name='model_name')" >> openspec/changes/fix-store-stage-full-schema-gap/pre_state.txt`
- [x] 1.3 Snapshot current absence of the two new indexes: `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT indexname FROM pg_indexes WHERE indexname IN ('idx_agent_tasks_model','idx_investigations_model')" >> openspec/changes/fix-store-stage-full-schema-gap/pre_state.txt`
- [x] 1.4 Capture worker logs proving the live failure: `docker logs --tail 80 zovark_mine-worker-1 2>&1 | grep -A1 "model_name" > openspec/changes/fix-store-stage-full-schema-gap/pre_state_worker.log`

## 2. Re-confirm Phase A against the live DB

- [x] 2.1 Re-run the `store.py` column audit (the same SQL we ran at propose time) and confirm the 2 model_name columns still show absent: save output to `openspec/changes/fix-store-stage-full-schema-gap/phase_a_recheck.txt`
- [x] 2.2 Confirm exactly 1 migration file (`047_add_model_name.sql`) appears in the gap-to-migration mapping; no surprise additions
- [x] 2.3 If any second migration file appears, STOP — investigate and update proposal/design before continuing

## 3. Build repair.sql and run the dry-run gate

- [x] 3.1 Compose `openspec/changes/fix-store-stage-full-schema-gap/repair.sql` with the verbatim DDL from `migrations/047_add_model_name.sql` plus the single `UPDATE schema_migrations` for that filename, all wrapped in `BEGIN; … COMMIT;`
- [x] 3.2 Compose `openspec/changes/fix-store-stage-full-schema-gap/repair_dryrun.sql` — identical to `repair.sql` except the trailing `COMMIT;` is replaced with `ROLLBACK;`
- [x] 3.3 Run the dry run: `docker exec -i zovark-postgres psql -U zovark -d zovark < openspec/changes/fix-store-stage-full-schema-gap/repair_dryrun.sql 2>&1 | tee openspec/changes/fix-store-stage-full-schema-gap/dry_run.out`
- [x] 3.4 Confirm `dry_run.out` contains 2× `ALTER TABLE`, 2× `CREATE INDEX`, 1× `UPDATE 1`, ends with `ROLLBACK`, and contains zero `ERROR:` lines
- [x] 3.5 If `dry_run.out` shows any `ERROR:` line, STOP — do not proceed to real apply

## 4. Real apply

- [x] 4.1 Apply: `docker exec -i zovark-postgres psql -U zovark -d zovark < openspec/changes/fix-store-stage-full-schema-gap/repair.sql 2>&1 | tee openspec/changes/fix-store-stage-full-schema-gap/repair.out`
- [x] 4.2 Confirm `repair.out` ends with `COMMIT` and contains no `ERROR:` lines
- [x] 4.3 Confirm `repair.out` shows exactly 1× `UPDATE 1` (the ledger UPDATE)

## 5. Schema verification gate

- [x] 5.1 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT model_name FROM agent_tasks LIMIT 1"` — must return without error
- [x] 5.2 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT model_name FROM investigations LIMIT 1"` — must return without error (zero or one row both fine)
- [x] 5.3 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT indexname FROM pg_indexes WHERE indexname IN ('idx_agent_tasks_model','idx_investigations_model') ORDER BY indexname"` — must return both index names
- [x] 5.4 `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT filename, source, applied_at FROM schema_migrations WHERE filename='047_add_model_name.sql'"` — must show `source='manual_backfill'` and a fresh `applied_at`

## 6. Pipeline verification gate

- [x] 6.1 Get a fresh JWT: `TOKEN=$(curl -s -X POST http://127.0.0.1:8090/api/v1/auth/login -H "Content-Type: application/json" -d '{"email":"admin@test.local","password":"TestPass2026"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')`
- [x] 6.2 Submit a synthetic `brute_force` task using the standard CLAUDE.md curl recipe and capture the returned `task_id` — task_id `7932cbfe-2237-46a4-b49a-f813ad91b439`
- [x] 6.3 Poll the task every 2 seconds for up to 60 seconds: `for i in $(seq 1 30); do sleep 2; STATUS=$(curl -s http://127.0.0.1:8090/api/v1/tasks/<TASK_ID> -H "Authorization: Bearer $TOKEN" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status"))'); echo "$i $STATUS"; [ "$STATUS" = "completed" ] && break; done`
- [ ] 6.4 **GATE FAILED** — task stayed `pending` for full 60s. NO `Store failed` line, but worker logs show `Pattern save failed (non-fatal): column "task_type" of relation "investigation_memory" does not exist` followed by `current transaction is aborted, commands ignored until end of transaction block` on the next two helper calls. Root cause: `store.py` uses one transaction per investigation, the investigation_memory schema fork (out of scope) aborts the txn, every later statement (including `conn.commit()`) is silently rolled back. The 047 fix is correct in isolation. Evidence: `section6_failure.log`. Halting per spec.
- [ ] 6.5 Skipped — gate 6.4 failed
- [ ] 6.6 Skipped — gate 6.4 failed
- [ ] 6.7 Skipped — gate 6.4 failed

## 7. Post-state evidence

- [ ] 7.1 Snapshot post-repair ledger state: append to `openspec/changes/fix-store-stage-full-schema-gap/post_state.txt`
- [ ] 7.2 Snapshot post-repair `\d agent_tasks` and `\d investigations` (model_name + indexes section) to `post_state.txt`
- [ ] 7.3 Capture clean worker logs proving Store now succeeds for the verifying task: `docker logs --since 10m zovark_mine-worker-1 2>&1 | grep -E "Started workflow|task_completed" | tail -20 > openspec/changes/fix-store-stage-full-schema-gap/post_state_worker.log`
- [ ] 7.4 Save the verifying task_id and its final JSON to `verify_task.json`

## 8. Document follow-up work (do NOT create as part of this change)

- [ ] 8.1 Add a one-line entry to `docs/RUNBOOK_HEALTHCHECK.md#schema-drift` linking this change as the second canonical example of code-derived gap analysis
- [ ] 8.2 Note (in this tasks.md, not as a filed issue) that `investigation_memory` schema fork remains and needs a separate code-vs-DB reconciliation change
- [ ] 8.3 Note (in this tasks.md, not as a filed issue) that the other 11 lying-ledger rows (033, 034, 038, 041_system_configs, 042, 043, 044, 048, 049, 051, 052, 053) remain dormant and should be audited per-stage as their consuming files are touched

## 9. Rollback plan (only if Section 6 verification fails)

- [x] 9.1 If §6.4 fails (task does not reach `completed`), capture the new `Store failed` log line to `openspec/changes/fix-store-stage-full-schema-gap/section6_failure.log` — captured (28 lines, includes root-cause analysis). Note: this run did NOT print `Store failed`; instead the failure mode is silent transaction poisoning via `current transaction is aborted` after `Pattern save failed (non-fatal)`.
- [x] 9.2 Do NOT drop the new columns or table — they are additive and idempotent — confirmed not dropped; §5 schema state stands intact
- [x] 9.3 Do NOT auto-revert the ledger UPDATE — the 047 repair is correct in isolation; a downstream failure would be a *third* drift to surface, not a reason to undo this fix — ledger UPDATE retained, 047 row remains `manual_backfill`
- [x] 9.4 Pause and report the new failure mode for user decision (widen scope, open follow-up change, or revert) — reporting now
