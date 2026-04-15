## ADDED Requirements

### Requirement: scripts/audit_init_sql_backfill.sh exists and is read-only
A new bash script `scripts/audit_init_sql_backfill.sh` SHALL exist with the following behaviour:

1. It SHALL accept `--help`, `--no-color`, `--json` flags. `--help` prints usage and exits 0. Unknown flags exit 2.
2. It SHALL read `migrations/072_schema_migrations_ledger.sql` and parse the fingerprint table delimited by `-- BEGIN FINGERPRINTS` and `-- END FINGERPRINTS` markers. The fingerprint table SHALL be a list of `(filename, kind, args)` triples where `kind ∈ {table, column, index}`.
3. For each row, it SHALL run a single read-only `psql -tAc` query against the live dev Postgres (via `docker compose exec -T`) that returns `t` if the fingerprint is present and `f` otherwise.
4. It SHALL emit one of:
   - **default mode**: a per-row table with columns `migration | kind | args | status` followed by a summary line `audit complete: N present, M missing`. Exit code 0 if M=0, 1 if M>0.
   - **`--json` mode**: a single JSON object `{schema_version: 1, total: N, present_count: P, missing_count: M, missing: ["filename1", ...]}`.
5. The script SHALL NOT issue any DDL or DML statements. It SHALL be safe to run against any Zovark Postgres including production.

#### Scenario: All fingerprints present (healthy DB)
- **WHEN** an operator runs `scripts/audit_init_sql_backfill.sh` against a DB where every init_sql-marked migration's fingerprint exists
- **THEN** the script prints a per-row table where every row is `present`, the summary line reads `audit complete: N present, 0 missing`, and the exit code is 0

#### Scenario: Drifted DB with several missing fingerprints
- **WHEN** an operator runs `scripts/audit_init_sql_backfill.sh` against a DB where 15 init_sql-marked migrations have missing fingerprints (e.g., `040_human_review_flags.sql` because `agent_tasks.needs_human_review` does not exist)
- **THEN** the script prints `MISSING` for each of those 15 rows, the summary line reads `audit complete: <N-15> present, 15 missing`, and the exit code is 1

#### Scenario: --json mode
- **WHEN** an operator runs `scripts/audit_init_sql_backfill.sh --json`
- **THEN** the script emits exactly one JSON object on stdout containing `schema_version`, `total`, `present_count`, `missing_count`, and a `missing` array of filenames; nothing else is printed; the exit code is 0 if `missing_count=0` and 1 otherwise

#### Scenario: No DB writes
- **WHEN** the script runs against a DB and is then immediately followed by a `psql -c "SELECT count(*), source FROM schema_migrations GROUP BY source"`
- **THEN** the row counts are unchanged from before the audit ran (the script makes no INSERT/UPDATE/DELETE)

### Requirement: scripts/repair_init_sql_backfill.sh DELETEs falsely-marked rows and re-runs the runner
A new bash script `scripts/repair_init_sql_backfill.sh` SHALL exist with the following behaviour:

1. It SHALL accept `--help`, `--no-color`, `--dry-run` flags. `--help` prints usage and exits 0. Unknown flags exit 2.
2. It SHALL invoke `scripts/audit_init_sql_backfill.sh --json` internally to compute the missing list.
3. In `--dry-run` mode: print one line per missing migration of the form `would DELETE schema_migrations row for <filename>`, print the count, exit 0 without modifying the DB.
4. In normal mode:
   - Open a single transaction (`BEGIN;`).
   - For each missing migration, issue `DELETE FROM schema_migrations WHERE filename = '<filename>'`.
   - Commit the transaction.
   - Invoke `scripts/apply_migrations.sh` (no flags) — this picks up the now-unmarked migrations and applies them with `source='migration_runner'`.
   - After the runner returns, re-invoke `scripts/audit_init_sql_backfill.sh --json` to verify `missing_count=0`.
   - Exit 0 on success, 1 on persistent drift, 1 on any psql or runner failure.
5. The script SHALL NOT modify any business tables. The DELETE only touches `schema_migrations`.

#### Scenario: --dry-run on a drifted DB
- **WHEN** an operator runs `scripts/repair_init_sql_backfill.sh --dry-run` on a DB with 15 missing fingerprints
- **THEN** the script prints exactly 15 `would DELETE` lines, prints `15 ledger rows would be deleted`, exits 0, and `SELECT count(*) FROM schema_migrations` returns the same number it did before the script ran

#### Scenario: Wet repair on a drifted DB
- **WHEN** an operator runs `scripts/repair_init_sql_backfill.sh` on the same drifted DB
- **THEN** the script DELETEs 15 rows, the runner applies the corresponding 15 migration files in numeric order (each in its own transaction), the final audit re-run reports `missing_count=0`, and the script exits 0

#### Scenario: Wet repair fails on a non-idempotent migration
- **WHEN** the runner errors on one of the now-unmarked migrations (e.g., `psql` returns SQLSTATE 42710 "object already exists" because of a hand-merged init.sql collision)
- **THEN** the runner's `--single-transaction` flag rolls back that file's changes, the runner exits 1 with the failing filename printed, the repair script propagates the exit code, and the ledger contains the rows for the migrations that succeeded BEFORE the failing one but NOT for the failing one itself or any after it

### Requirement: migration 072 backfill is fingerprint-conditional, not unconditional
`migrations/072_schema_migrations_ledger.sql` SHALL replace its current static `INSERT … VALUES (...)` block with a `DO $$` block that, for each candidate `(filename, kind, args)` triple in a hand-curated `VALUES` table, evaluates the fingerprint against `information_schema` / `pg_indexes` / `pg_class` and only INSERTs the ledger row when the fingerprint is present. The `VALUES` table SHALL be delimited by `-- BEGIN FINGERPRINTS` and `-- END FINGERPRINTS` comment markers so the audit script can parse it. The migration SHALL remain idempotent (running it twice is a no-op).

#### Scenario: Fresh dev volume with stale init.sql
- **WHEN** an operator runs `docker compose down -v && docker compose up -d` (so init.sql runs against a fresh volume) and then `scripts/apply_migrations.sh`, against an `init.sql` that is hand-merged through migration 032 only
- **THEN** the runner applies migration 072, the `DO $$` block iterates through its candidate list, INSERTs ledger rows ONLY for the migrations whose fingerprints are observable (roughly 000-032 plus a handful of hand-merged-in ones from the 033-053 range), and the runner then applies the remaining unmarked init_sql-era migrations in numeric order via `source='migration_runner'`. The final ledger has every migration recorded with the correct source.

#### Scenario: Re-run on an already-correct ledger
- **WHEN** an operator re-applies `migrations/072_schema_migrations_ledger.sql` against a DB whose ledger is already in sync
- **THEN** the migration runs without error, the `DO $$` block re-evaluates every fingerprint, every INSERT is a no-op due to `ON CONFLICT (filename) DO NOTHING`, and `SELECT count(*) FROM schema_migrations` returns the same number it did before

#### Scenario: 072's fingerprint table is parseable by the audit script
- **WHEN** `scripts/audit_init_sql_backfill.sh` reads `migrations/072_schema_migrations_ledger.sql`
- **THEN** the script extracts every line between `-- BEGIN FINGERPRINTS` and `-- END FINGERPRINTS`, parses the `('filename', 'kind', 'args')` tuples, and runs the appropriate query for each. The set of fingerprints the audit checks SHALL exactly equal the set of candidates the `DO $$` block iterates over.

### Requirement: Operator runbook documents the audit + repair workflow
The existing `docs/RUNBOOK_HEALTHCHECK.md#schema-drift` section SHALL be extended with a "Fingerprint mismatch (the ledger lies)" subsection that documents:

- The symptom (worker fails at Stage 5 STORE with `42703 column does not exist` even though `apply_migrations.sh --dry-run` reports no pending migrations; OR Stage 0.7 e2e probe fail).
- The diagnostic (`scripts/audit_init_sql_backfill.sh`).
- The dry-run (`scripts/repair_init_sql_backfill.sh --dry-run`).
- The fix (`scripts/repair_init_sql_backfill.sh`).
- A cross-link to the existing top of the `#schema-drift` section.
- An explicit warning that the repair script DELETEs ledger rows and that operators should review the dry-run output before running the wet repair.

#### Scenario: Operator follows the link from a worker error
- **WHEN** an operator sees `Store failed: column "X" of relation "agent_tasks" does not exist` in the worker logs and opens the runbook at `#schema-drift`
- **THEN** the section's "Fingerprint mismatch" subsection contains all five items above and a copy-pasteable command sequence

## MODIFIED Requirements

### Requirement: Probe inserts a Stage 0.6 schema_ledger between db_write and ingest
`scripts/e2e_probe.sh` SHALL define `stage06_schema_ledger()` AND `stage07_schema_fingerprint()`. Stage 0.6 runs after `stage05_db_write()` and before `stage07_schema_fingerprint()`. Stage 0.7 runs after `stage06_schema_ledger()` and before `stage1_ingest()`. The full stage runner sequence SHALL be: `0 login`, `0.5 db_write`, `0.6 schema_ledger`, `0.7 schema_fingerprint`, `1 ingest`, `2 redpanda`, `3 pg.investigating`, `4 pg.completed`, `5 signoz`, `6 verdict`, `7 cleanup`. The script's `usage()` / banner block SHALL list all eleven stages in order.

#### Scenario: Stage 0.7 runs after Stage 0.6
- **WHEN** an operator runs `scripts/e2e_probe.sh` on a healthy stack
- **THEN** the timeline shows `0.5 db_write`, `0.6 schema_ledger`, `0.7 schema_fingerprint`, `1 ingest` in that order, all four marked `pass`

#### Scenario: Stage 0.7 fails — Stage 1 is skipped
- **WHEN** Stage 0.7 records `fail` (any init_sql fingerprint is missing)
- **THEN** Stage 1 `ingest` records `skip` with detail `prior stage failed`, no HTTP request is sent to `POST /api/v1/tasks`, and the script exits non-zero

### Requirement: Stage 0.7 schema_fingerprint runs the audit script and parses the missing count
`scripts/e2e_probe.sh` Stage 0.7 SHALL invoke `scripts/audit_init_sql_backfill.sh --json` and parse the `missing_count` field from the JSON output. Stage 0.7 SHALL emit `pass` only when `missing_count = 0`. On `missing_count > 0`, Stage 0.7 SHALL emit `fail` with detail `missing=N — run scripts/repair_init_sql_backfill.sh` and print a one-line stderr hint pointing at `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`.

#### Scenario: Healthy DB
- **WHEN** Stage 0.7 runs against a DB where every init_sql fingerprint is present
- **THEN** the stage records `pass` with detail `applied=N missing=0` and the script proceeds to Stage 1

#### Scenario: 15 missing fingerprints
- **WHEN** Stage 0.7 runs against a DB where 15 init_sql-marked migrations have missing fingerprints
- **THEN** the stage records `fail` with detail `missing=15 — run scripts/repair_init_sql_backfill.sh` and the stderr hint points at the runbook anchor
