## 1. Hand-curate the fingerprint table for all 58 init_sql-era migrations

- [ ] 1.1 Open every file in the current 072 backfill list (000_init_agent_tasks.sql through 053_ioc_evidence_refs.sql, plus the four duplicates at 041 / 050 / 051 / 052 — 58 files total). For each, identify a single representative DDL effect: a `CREATE TABLE foo` (kind=table, args=foo), an `ALTER TABLE foo ADD COLUMN bar` (kind=column, args=foo|bar), or a `CREATE INDEX idx_foo` (kind=index, args=idx_foo). Prefer column over table when the migration's primary purpose is to add a column to an existing table.
- [ ] 1.2 Build the fingerprint table as a tab-separated list and save to `/tmp/fingerprints.tsv` for review (filename, kind, args). 58 rows.
- [ ] 1.3 For each row, sanity-check: open the migration file and confirm the DDL really matches the fingerprint encoding. Note any migrations that have NO observable DDL effect (e.g., `039_drop_legacy_tables.sql` only DROPs things) — these need a special handling rule.
- [ ] 1.4 Decide the rule for migrations whose DDL is purely DROP / cleanup / data: encode them as `kind=always_present` so the audit treats them as never-missing (they have no fingerprint to verify). Add `always_present` to the kind enum in §3 and §4.

## 2. scripts/audit_init_sql_backfill.sh

- [ ] 2.1 Create `scripts/audit_init_sql_backfill.sh` with the standard preamble (`#!/usr/bin/env bash`, `set -euo pipefail`, `MSYS_NO_PATHCONV=1`, `--help`, `--no-color`, `--json` flag parser).
- [ ] 2.2 Implement the fingerprint-table parser: reads `migrations/072_schema_migrations_ledger.sql`, finds the lines between `-- BEGIN FINGERPRINTS` and `-- END FINGERPRINTS`, extracts the `('filename', 'kind', 'args')` tuples via a single `awk` or `sed` pipeline. The parser must tolerate inline SQL comments and trailing commas.
- [ ] 2.3 Implement `check_table(name)`, `check_column(table, col)`, `check_index(name)`, `check_always_present()` helpers that each issue one `psql -tAc` query and return `t` or `f`.
- [ ] 2.4 Loop through the parsed tuples, dispatch on `kind`, accumulate `present` and `missing` arrays.
- [ ] 2.5 Implement default output: a per-row table with `migration | kind | args | status` columns, sorted by filename, color-coded (`MISSING` in red, `present` in green) unless `--no-color`. Final summary line `audit complete: N present, M missing`.
- [ ] 2.6 Implement `--json` output: emit one JSON object via `jq -n` with `schema_version, total, present_count, missing_count, missing` fields. The `missing` array is sorted by filename for stable diffs.
- [ ] 2.7 Implement exit-code logic: 0 if `missing_count = 0`, 1 if `missing_count > 0`, 2 on any internal error (parse failure, psql failure, etc.).
- [ ] 2.8 `bash -n scripts/audit_init_sql_backfill.sh` parses cleanly. Verify with `--help` against the live stack — must print usage and exit 0 without touching the DB.
- [ ] 2.9 Run `scripts/audit_init_sql_backfill.sh` against the current dev DB. Capture the missing list. Expected: roughly 15+ missing migrations (matching the spot-check in the proposal).

## 3. Rewrite migrations/072 backfill block as fingerprint-conditional

- [ ] 3.1 Replace the existing 58-row `INSERT … VALUES (...)` block in `migrations/072_schema_migrations_ledger.sql` with a `DO $$ … $$` block that iterates over the fingerprint table and conditionally INSERTs.
- [ ] 3.2 Wrap the fingerprint table in `-- BEGIN FINGERPRINTS` / `-- END FINGERPRINTS` markers so the audit script can parse the same data.
- [ ] 3.3 Implement the kind-based dispatch inside the `DO $$` block: `table → to_regclass`, `column → information_schema.columns`, `index → pg_indexes`, `always_present → TRUE`.
- [ ] 3.4 Update the migration's leading comment block to document the new behaviour: "init.sql backfill is fingerprint-conditional; rows are only inserted when the corresponding schema object actually exists in the live DB at backfill time."
- [ ] 3.5 Verify idempotency: apply migration 072 against the current DB. The existing 58 init_sql rows should be untouched (every fingerprint that's currently present stays present); rows whose fingerprint is missing should NOT be re-inserted (they were inserted by the previous bad version of 072 and will be DELETEd by the repair script in §6 anyway). Use `EXPLAIN ANALYZE` if the `DO $$` block needs perf scrutiny — it shouldn't (58 simple lookups).

## 4. scripts/repair_init_sql_backfill.sh

- [ ] 4.1 Create `scripts/repair_init_sql_backfill.sh` with the standard preamble and `--help`, `--no-color`, `--dry-run` flags.
- [ ] 4.2 Run `scripts/audit_init_sql_backfill.sh --json` internally, parse the `missing` array.
- [ ] 4.3 If `missing_count = 0`, print "no repair needed" and exit 0.
- [ ] 4.4 In `--dry-run` mode: print one `would DELETE schema_migrations row for <filename>` line per missing migration, print the count, exit 0 without touching the DB.
- [ ] 4.5 In wet mode:
  - Open a single transaction.
  - For each missing filename, execute `DELETE FROM schema_migrations WHERE filename = '<filename>'` and verify exactly one row was deleted (else fail loudly).
  - Commit the transaction.
  - Invoke `scripts/apply_migrations.sh` with no flags. Capture stdout/stderr.
  - Re-run the audit. If `missing_count = 0`, print success and exit 0. If still non-zero, print the persistent missing list and exit 1.
- [ ] 4.6 Print a clear separator between phases (`=== audit ===`, `=== delete ===`, `=== apply ===`, `=== verify ===`) so an operator can see exactly where they are if the script pauses or fails.
- [ ] 4.7 `bash -n scripts/repair_init_sql_backfill.sh` parses cleanly.

## 5. e2e probe Stage 0.7

- [ ] 5.1 In `scripts/e2e_probe.sh`, add `stage07_schema_fingerprint()` between `stage06_schema_ledger()` and `stage1_ingest()`.
- [ ] 5.2 Implementation: invoke `$(dirname "$0")/audit_init_sql_backfill.sh --json`, capture stdout to a var. If the script returns non-zero AND emits no parseable JSON, record `fail` with `audit script error: <head -c 100>` and return 1.
- [ ] 5.3 Parse `missing_count` via `jq -r '.missing_count // 0'`. If `missing_count = 0`, record `pass` with detail `applied=$total missing=0`. Else record `fail` with detail `missing=$missing_count — run scripts/repair_init_sql_backfill.sh` and print a stderr hint pointing at `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`.
- [ ] 5.4 In the stage runner loop near `e2e_probe.sh` line ~772, add `stage07_schema_fingerprint` to the sequence between `stage06_schema_ledger` and `stage1_ingest`.
- [ ] 5.5 Update the script's banner / `usage()` block to insert `0.7. schema_fingerprint — verify init_sql-marked migrations actually exist in the DB` between `0.6. schema_ledger` and `1. ingest`.
- [ ] 5.6 `bash -n scripts/e2e_probe.sh` parses cleanly.

## 6. Apply the audit + repair to the live dev DB

- [ ] 6.1 Take a `pg_dump` snapshot: `docker compose exec -T postgres pg_dump -U zovark zovark | gzip > /tmp/zovark-pre-init-sql-repair-$(date +%Y%m%d-%H%M%S).sql.gz`. Document the path in §8.
- [ ] 6.2 Run `scripts/audit_init_sql_backfill.sh` (default mode). Capture the full output to a file. Manually review every `MISSING` row and confirm the fingerprint matches the file's actual DDL — this catches encoding mistakes from §1.
- [ ] 6.3 Run `scripts/audit_init_sql_backfill.sh --json` and stash the JSON for §8 records.
- [ ] 6.4 Run `scripts/repair_init_sql_backfill.sh --dry-run`. Verify the DELETE list matches the audit's missing list exactly.
- [ ] 6.5 Run `scripts/repair_init_sql_backfill.sh` (wet). Watch the four phases (audit / delete / apply / verify). On any pause, capture the failing migration name and the psql stderr tail.
- [ ] 6.6 If §6.5 succeeds, the final audit should report `0 missing`. If §6.5 fails on a non-idempotent migration, PAUSE the change, document the failure in §10, and report.
- [ ] 6.7 Verify the previously-broken column exists: `docker compose exec -T postgres psql -U zovark -d zovark -c "\d agent_tasks" | grep needs_human_review`.
- [ ] 6.8 Restart the worker (`docker compose restart worker`) and confirm `docker compose logs worker --tail 20` no longer contains `Store failed: column "needs_human_review"`.

## 7. Runbook + CLAUDE.md updates

- [ ] 7.1 In `docs/RUNBOOK_HEALTHCHECK.md`, find the existing `<a id="schema-drift"></a>` section. Add a new `### Fingerprint mismatch (the ledger lies)` subsection after the existing fix block.
- [ ] 7.2 Document the symptom (worker fails at Stage 5 STORE with `42703 column does not exist` even though `apply_migrations.sh --dry-run` reports no pending; OR Stage 0.7 fails with `missing=N`).
- [ ] 7.3 Document the diagnostic command (`scripts/audit_init_sql_backfill.sh`).
- [ ] 7.4 Document the dry-run command (`scripts/repair_init_sql_backfill.sh --dry-run`).
- [ ] 7.5 Document the fix command (`scripts/repair_init_sql_backfill.sh`) with an explicit warning about reviewing the dry-run output first.
- [ ] 7.6 Cross-link from the existing top of `#schema-drift` ("if `--dry-run` reports no pending but the worker still fails, see the Fingerprint mismatch subsection below").
- [ ] 7.7 Update `CLAUDE.md` Known Issue #13 to mention both scripts and the Stage 0.7 fingerprint check (currently mentions only the Stage 0.6 ledger check).

## 8. Verification

- [ ] 8.1 Restart the API: `docker compose restart api`. Confirm `docker logs zovark-api 2>&1 | grep schema_migrations_check` still shows `status=present applied=N` (no regression of the previous change's drift check).
- [ ] 8.2 Run `scripts/audit_init_sql_backfill.sh`. Expected: `audit complete: 58 present, 0 missing` (or whatever the new total is after §6 applied the missing migrations).
- [ ] 8.3 Run `scripts/e2e_probe.sh --signoz-required false`. Expected: all eleven stages pass (0, 0.5, 0.6, 0.7, 1, 2, 3, 4, 5, 6, 7). Capture the full timeline output.
- [ ] 8.4 Run `scripts/smoke_test_100.sh`. Detection rate must be ≥ baseline.
- [ ] 8.5 Negative test: `docker compose exec -T postgres psql -U zovark -d zovark -c "ALTER TABLE agent_tasks DROP COLUMN needs_human_review"`. Run `scripts/audit_init_sql_backfill.sh`. Expected: exit 1, `040_human_review_flags.sql` reported as `MISSING`. Restore: `scripts/repair_init_sql_backfill.sh`.
- [ ] 8.6 Negative test: re-apply migration 072 (`docker compose exec -T postgres psql ... < migrations/072_schema_migrations_ledger.sql`). Verify row counts unchanged (idempotency).

## 9. Cleanup

- [ ] 9.1 `git status` — confirm only the files listed in §1-§7 are modified or added.
- [ ] 9.2 Update the change's own task checkboxes in this file as work progresses.
- [ ] 9.3 If §6.5 turned up a non-idempotent migration that requires manual handling, file it as a follow-up change.

## 10. Deviations and follow-ups

- [ ] 10.1 (placeholder — populate during /opsx:apply with any decisions that diverge from the proposal/design)
