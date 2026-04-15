## 1. Migration 072 — schema_migrations ledger + init.sql backfill

- [x] 1.1 Generate the canonical init.sql-era filename list. **Deviation:** spec said files numbered `000_*` through `055_*`, but Phase 1 audit confirmed `054_cipher_audit_events` and `055_template_promotion` are absent from the dev DB — so init.sql does NOT bake them in. Backfill range is `000_*` through `053_*` plus the four duplicate-numbered files at 041 / 050 / 051 / 052 = **58 rows total**. Proposal and spec updated to match.
- [x] 1.2 Created `migrations/072_schema_migrations_ledger.sql` with `CREATE TABLE IF NOT EXISTS schema_migrations` (filename PK, applied_at, applied_by, source enum, checksum) and 58 backfill rows wrapped in `BEGIN; ... COMMIT;`.
- [x] 1.3 Header comment added — explains what / why / sentinel timestamp / runbook anchor.
- [x] 1.4 Idempotency verified live: applied to dev DB twice in a row, second run logs `INSERT 0 0` and `SELECT count(*)` stays at 58.

## 2. scripts/apply_migrations.sh — strict bash runner

- [x] 2.1 **Deviation:** `scripts/apply_migrations.sh` already existed (Ticket 6) with `--from`/`--to`/`--include-068` flags and a critical `APPLY-068` interactive safety gate around the SurrealDB cutover migration. Decision: extend in place rather than replace, preserving all existing flags + the 068 gate, while adding ledger awareness.
- [x] 2.2 `discover_migrations` is implemented inline as `printf '%s\n' "${all_files[@]}" | sort -V` — no separate function (kept inline to match the existing script's style).
- [x] 2.3 `is_applied(filename)` implemented; uses `to_regclass('public.schema_migrations')` for ledger-existence check and treats absent table as "not applied".
- [x] 2.4 `apply_one(filename)` implemented; uses `--single-transaction` and ON_ERROR_STOP=1, computes sha256 host-side, captures stderr to a tempfile, prints last 20 lines on failure and exits 1. Ledger INSERT is done as a separate post-commit step so migrations like 072 that contain their own COMMIT don't conflict.
- [x] 2.5 Bootstrap path for missing ledger: when `ledger_exists` returns false, the to-apply list contains only 072. After applying it, the script re-scans and picks up the rest.
- [x] 2.6 `--dry-run` implemented; prints to-apply list and exits 0 without psql traffic.
- [x] 2.7 `--mark-applied <filename> <source>` implemented; validates source enum, file existence, and the ledger's existence before the INSERT.
- [x] 2.8 `bash -n scripts/apply_migrations.sh` parses cleanly. Existing `chmod +x` is preserved.
- [x] 2.9 Spot-checked 13 of the 14 unapplied migrations (skipping 068 which is gated). All `CREATE TABLE` / `ALTER TABLE` statements use `IF NOT EXISTS`. The few `ADD CONSTRAINT` statements are paired with `DROP CONSTRAINT IF EXISTS` (idempotent in pairs). No bare `CREATE TABLE foo` found. Each migration runs in its own transaction so a failure rolls back cleanly.

## 3. API drift check

- [x] 3.1 `checkSchemaMigrationsLedger(ctx, pool)` added to `api/db.go`. Returns `(int, error)`.
- [x] 3.2 Three outcomes implemented: (a) success with N → `(N, nil)`; (b) `42P01` → `(0, nil)`; (c) other errors → `(0, err)`.
- [x] 3.3 Wired into `initDB` after `selfTestPool`. 5-second context timeout. `requireSchemaLedger()` helper reads `ZOVARK_REQUIRE_SCHEMA_LEDGER` and accepts `true`/`TRUE`/`True`/`1`/`yes`/`YES`.
- [x] 3.4 Log lines as specified: `INFO schema_migrations_check status=present applied=N` / `WARN ... status=absent hint=...` / `ERROR ... status=absent hint=...` + return error.
- [x] 3.5 **Deviation:** test added to existing `api/admin_handlers_test.go` rather than a new `api/db_drift_test.go` file (consolidates with sibling tests for the previous change). Function: `TestRequireSchemaLedger_EnvParsing` — covers true/TRUE/True/1/yes/YES + unset/false/0/no/foobar.
- [x] 3.6 `go build ./api/...` clean.
- [x] 3.7 `go test -run 'TestRequireSchemaLedger|TestParseQueryExecMode|TestProbeDBHandler' ./api/...` — all pass.

## 3b. Pre-existing api/migrate.go integration

- [x] 3b.1 **Deviation:** `api/migrate.go` already existed with its own `schema_migrations(version VARCHAR(255))` schema — never run in dev (no table existed) but referenced by the legacy `apply_migrations.sh api` mode and the `./hydra-api migrate up` CLI subcommand. Updated `ensureMigrationTable` to use the richer 5-column schema (filename PK, applied_at, applied_by, source enum, checksum) so this code path and the bash runner write to the same ledger.
- [x] 3b.2 `getAppliedMigrations` updated to `SELECT filename FROM schema_migrations`.
- [x] 3b.3 The `INSERT` in `migrateUp` updated to write `(filename, applied_by, source='migration_runner', checksum='')` with `ON CONFLICT (filename) DO NOTHING`.

## 4. e2e probe Stage 0.6

- [x] 4.1 `stage06_schema_ledger()` added between `stage05_db_write` and `stage1_ingest`. Uses `comm -23 <(disk) <(ledger)` for the drift comparison.
- [x] 4.2 `42P01` path handled: detects `does not exist` in stderr, records `fail` with `ledger absent — run scripts/apply_migrations.sh`, prints stderr hint with runbook anchor.
- [x] 4.3 Empty-drift path records `pass` with `applied=N drift=0`.
- [x] 4.4 Non-empty-drift path records `fail` with `drift: <first 5>` and prints stderr hint with the count.
- [x] 4.5 `stage06_schema_ledger` added to the runner array between `stage05_db_write` and `stage1_ingest`.
- [x] 4.6 Stage list comment updated to insert `0.6. schema_ledger`.
- [x] 4.7 `bash -n scripts/e2e_probe.sh` parses cleanly.
- [x] 4.8 **Deviation found in live testing:** Stage 0.6 initially failed against the healthy stack because `068_ticket2_surreal_graph_pgvector_retirement.sql` is on disk but intentionally NOT in the ledger (SurrealDB cutover gate). Updated `stage06_schema_ledger` to exclude `068_*` from the on-disk list with an inline comment explaining the gate. Verified manually: `comm -23` produces empty drift after the exclusion.

## 5. Runbook + CLAUDE.md

- [x] 5.1 `<a id="schema-drift"></a>` anchor + `## POST /api/v1/tasks returns 500 with 42703 column does not exist (schema drift)` heading added.
- [x] 5.2 Symptom section documents 42703 line, schema_migrations_check WARN line, and Stage 0.6 fail mode.
- [x] 5.3 Diagnostic: `scripts/apply_migrations.sh --dry-run`.
- [x] 5.4 Fix: `scripts/apply_migrations.sh`.
- [x] 5.5 Production strict-mode section added with explicit "do not set on dev volumes" warning.
- [x] 5.6 Cross-reference to `#api-08p01` added (the prior change unmasked this one).
- [x] 5.7 `CLAUDE.md` Known Issues #13 added.
- [x] 5.8 `## Database` section gained the `apply_migrations.sh` line.
- [x] 5.9 **Bonus:** added a "068 SurrealDB cutover gate" subsection to the runbook explaining when operators will see "Skipping 068" and why that's correct.

## 6. Migration application — first dry run

- [x] 6.1 First dry-run produced 15 entries (the expected 14 + 070 because 070 wasn't pre-marked yet). Pre-marked 070 as `manual_backfill` via `scripts/apply_migrations.sh --mark-applied 070_probe_writes_table.sql manual_backfill`. Second dry-run produced exactly 14 entries: 054, 055, 059–067, 069, 071, 072. **068 correctly skipped** with the safety-gate notice line.
- [x] 6.2 No extras leaked in. `seed_dev_data.sql` excluded by `! -name 'seed_*'`. 070 excluded after pre-marking.
- [x] 6.3 Spot-checked all 14 files. All `CREATE TABLE`/`ALTER TABLE … ADD COLUMN` use `IF NOT EXISTS`. `ADD CONSTRAINT` is paired with `DROP CONSTRAINT IF EXISTS`. No bare DDL.

## 7. Migration application — wet run

- [x] 7.1 `pg_dump` snapshot at `/tmp/zovark-pre-migration-072-20260413-122912.sql.gz` (26 KB).
- [x] 7.2 Wet run applied **14 migrations** in this order: 054, 055, 059, 060, 061, 062, 063, 064, 065, 066, 067, 069, 071, 072. Final line: `applied 14 migration(s); ledger now has 73 row(s)`.
- [x] 7.3 070 was pre-marked in §6.1 already, so this step was unneeded.
- [x] 7.4 Ledger breakdown verified: `init_sql 58`, `manual_backfill 1`, `migration_runner 14`. Total 73.
- [x] 7.5 New columns verified in `agent_tasks`: `trace_id`, `dedup_count`, `path_taken` are all present (with their indexes). **Deviation:** the proposal listed `plan_executed` and `execution_mode` as columns added to `agent_tasks` by 062 — that was a documentation error. `execution_mode` is on `agent_skills`, not `agent_tasks`; `plan_executed` does not exist anywhere in migrations. The columns the API actually references (per `api/task_handlers.go`) are `trace_id`, `dedup_count`, `path_taken`, all present. Functional gap: none.

## 8. Verification

- [x] 8.1 API rebuilt + restarted. Logs show `schema_migrations_check status=present applied=73`.
- [x] 8.2 No regression: `pgx_pool_query_mode mode=describe_exec` and `pgx_pool_self_test result=passed` both still present in the same boot logs.
- [ ] 8.3 **DEFERRED.** Stages 0, 0.5, 0.6, 1 verified working: probe row created at `pending` (proves Stage 1 ingest succeeded), Stage 0.6 logic produces `drift=0` when reproduced manually, Stage 0.5 db_write returned `took_ms=46 row_id=...` in the live run. **Stage 3 blocks on a pre-existing unrelated worker bug**: `worker/stages/store.py:102 NameError: name 'contextmanager' is not defined`. Worker container is in CrashLoop. This is a separate bug, exposed because this change unblocked the API path that previously failed at the schema layer. Tracked as a follow-up. Captured timeline output is unavailable because the probe renders its table only after all stages complete.
- [ ] 8.4 **DEFERRED.** Same blocker as §8.3 — smoke test depends on the worker actually consuming alerts.
- [x] 8.5 Negative drift test: `DELETE FROM schema_migrations WHERE filename = '061_trace_id.sql'` produced 1 row deleted; Stage 0.6 logic (reproduced manually) reports `drift: 061_trace_id.sql`. Restored via `--mark-applied 061_trace_id.sql migration_runner`.
- [x] 8.6 Negative strict-env test: dropped `schema_migrations` table, ran the API container with `-e ZOVARK_REQUIRE_SCHEMA_LEDGER=true`. Logs show `pgx_pool_query_mode mode=describe_exec` ✓, `pgx_pool_self_test result=passed` ✓, `ERROR schema_migrations_check status=absent hint="...run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift"` ✓, `Failed to initialize database: schema_migrations ledger absent...` ✓. **The API did NOT bind the listener** (no `Listening and serving HTTP on :8090`, no Gin route registration). Restored via `scripts/apply_migrations.sh` (recreates the ledger via 072 + re-applies the 14).

## 9. Cleanup

- [x] 9.1 Files modified/added: `migrations/072_schema_migrations_ledger.sql` (new), `scripts/apply_migrations.sh` (extended), `api/db.go` (drift check), `api/migrate.go` (schema sync), `api/admin_handlers_test.go` (TestRequireSchemaLedger), `scripts/e2e_probe.sh` (Stage 0.6), `docs/RUNBOOK_HEALTHCHECK.md` (#schema-drift), `CLAUDE.md` (Known Issue #13 + Database section), plus openspec change artifacts.
- [x] 9.2 All applicable checkboxes flipped. Three remain `[ ]` explicitly deferred (§8.3, §8.4 worker crash; the deferral notes spell out exactly why and what's next).
- [x] 9.3 §8.3 surfaced a new failure: `worker/stages/store.py:102 NameError: name 'contextmanager' is not defined`. Tracked as a follow-up change. Not bundled here.

## 10. Deviations from the original proposal

- [x] 10.1 **Backfill range was 000–053, not 000–055.** Phase 1 audit confirmed `054_cipher_audit_events.sql` and `055_template_promotion.sql` are absent from init.sql; both are applied by the runner. Proposal/spec updated.
- [x] 10.2 **`scripts/apply_migrations.sh` already existed** with `--from`/`--to`/`--include-068` flags and a critical `APPLY-068` interactive safety gate (SurrealDB cutover). Decision: extend in place, preserving all existing flags + the gate, while adding ledger awareness, `--dry-run`, and `--mark-applied`.
- [x] 10.3 **`api/migrate.go` already existed** as a `./hydra-api migrate up` CLI subcommand using a 2-column `schema_migrations(version, applied_at)` schema. Never run in dev (no table existed). Updated to use the same 5-column schema as migration 072 so the bash runner and the Go path write to the same ledger.
- [x] 10.4 **The unit test for `requireSchemaLedger` lives in `api/admin_handlers_test.go`**, not in a new `api/db_drift_test.go`. Reason: the hermetic test setup helpers and the `contains` helper are already in `admin_handlers_test.go` from the previous change; adding a new test file would have required duplicating them.
- [x] 10.5 **Stage 0.6 excludes `068_*` from the on-disk list** because 068 is intentionally gated behind `--include-068`. Without this exclusion, Stage 0.6 would falsely report drift on every healthy stack until SurrealDB cutover.
- [x] 10.6 **§8.3 e2e and §8.4 smoke test deferred.** Blocked by an unrelated pre-existing worker bug (`worker/stages/store.py:102 NameError: contextmanager`). The migration fix itself is verified by §7.4 (ledger row counts), §7.5 (columns present), §8.1 (API boots cleanly with the drift check passing), §8.5 (drift detection), and §8.6 (strict-env fail-fast).
