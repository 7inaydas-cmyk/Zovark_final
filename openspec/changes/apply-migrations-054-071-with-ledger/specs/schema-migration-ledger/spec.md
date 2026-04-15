## ADDED Requirements

### Requirement: schema_migrations ledger table exists with backfilled init.sql era
The Zovark Postgres SHALL contain a `schema_migrations` table with the following schema:

```sql
CREATE TABLE schema_migrations (
    filename     TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by   TEXT        NOT NULL DEFAULT current_user,
    source       TEXT        NOT NULL CHECK (source IN ('init_sql', 'migration_runner', 'manual_backfill')),
    checksum     TEXT
);
```

The table SHALL be created by `migrations/072_schema_migrations_ledger.sql`. The same migration SHALL pre-populate the ledger with one row per `migrations/*.sql` file numbered `000_*` through `053_*` (inclusive of the four duplicate-numbered files at 041 / 050 / 051 / 052; 58 rows total). Each backfill row SHALL have `source = 'init_sql'`, `applied_at = '2026-01-01T00:00:00Z'`, and `applied_by = 'init_sql_freeze_2026-04-13'`. The backfill INSERT SHALL use `ON CONFLICT (filename) DO NOTHING` so the migration is idempotent. Migrations `054_cipher_audit_events.sql` and `055_template_promotion.sql` SHALL NOT be in the backfill — both are missing from `init.sql` and are applied by the runner.

#### Scenario: Fresh dev volume after migration 072
- **WHEN** an operator runs `docker compose down -v && docker compose up -d`, lets `init.sql` run, then runs `scripts/apply_migrations.sh` (which applies migration 072 first)
- **THEN** `SELECT count(*) FROM schema_migrations WHERE source='init_sql'` returns 58, and `SELECT count(*) FROM schema_migrations WHERE source='migration_runner'` returns 15 (072 itself + 054, 055, 059, 060, 061, 062, 063, 064, 065, 066, 067, 068, 069, 071)

#### Scenario: Re-running migration 072 is a no-op
- **WHEN** an operator applies `migrations/072_schema_migrations_ledger.sql` against a DB that already has the ledger populated
- **THEN** the migration completes successfully, `schema_migrations` retains its existing rows, and no duplicate-key error is raised (the backfill INSERT uses `ON CONFLICT DO NOTHING`)

### Requirement: scripts/apply_migrations.sh applies pending migrations in deterministic order
A new bash script `scripts/apply_migrations.sh` SHALL exist with the following behaviour:

1. It SHALL accept `--help`, `--no-color`, `--dry-run`, and `--mark-applied <filename> <source>` flags. `--help` prints usage and exits 0. Any unknown flag prints usage to stderr and exits 2.
2. It SHALL discover migration files via `find migrations -maxdepth 1 -name '*.sql' ! -name 'seed_*'` and sort them by **numeric prefix first, then full filename lexicographic** (so duplicate-numbered files have a deterministic order).
3. For each file, it SHALL query `SELECT 1 FROM schema_migrations WHERE filename = $1` and add the file to a "to apply" list only if absent.
4. The script SHALL special-case `072_schema_migrations_ledger.sql` so that, on the first invocation when `schema_migrations` does not yet exist, the script runs migration 072 BEFORE attempting any ledger queries. After 072 runs, the script inserts a row for 072 itself with `source='migration_runner'` and continues.
5. For every other file in the to-apply list, the script SHALL open a transaction, run the file via `psql \i`, compute the file's `sha256sum`, INSERT the ledger row with `source='migration_runner'` and the checksum, then COMMIT. On any psql non-zero exit, the transaction SHALL roll back and the script SHALL exit non-zero with the failed filename and the last 20 lines of psql stderr printed.
6. With `--dry-run`, the script SHALL print the to-apply list and exit 0 without applying anything.
7. With `--mark-applied <filename> <source>`, the script SHALL insert a row into `schema_migrations` for that filename with the given source ('init_sql', 'migration_runner', or 'manual_backfill') and the file's current sha256, then exit 0. Used to backfill known-applied files.
8. The script SHALL be `set -euo pipefail` and idempotent (running it twice in a row applies zero net migrations the second time).

#### Scenario: Dry run on a drifted dev volume
- **WHEN** an operator runs `scripts/apply_migrations.sh --dry-run` on a dev volume that has only `init.sql` applied and has just had migration 072 + the 14 unapplied migrations checked in
- **THEN** the script prints a list containing exactly the 14 unapplied filenames (054, 055, 059, 060, 061, 062, 063, 064, 065, 066, 067, 068, 069, 071) plus `072_schema_migrations_ledger.sql`, in numeric-then-lex order, and exits 0 without modifying the DB

#### Scenario: Wet run applies the gap and records the ledger
- **WHEN** an operator runs `scripts/apply_migrations.sh` against the same drifted volume from the previous scenario
- **THEN** the script applies all 15 migrations in order, prints "applied 15 migrations" on completion, exits 0, and `SELECT count(*) FROM schema_migrations WHERE source='migration_runner'` returns at least 15

#### Scenario: Wet run with one bad migration in the middle
- **WHEN** the runner is mid-way through the to-apply list and `psql` returns non-zero on `migrations/063_system_tenant.sql` (e.g., a typo in the file)
- **THEN** the transaction for 063 rolls back, no ledger row is inserted for 063, the script exits 1 with the filename and the psql stderr tail printed, and the migrations applied before 063 (054, 055, 059, 060, 061, 062) remain committed with their ledger rows

#### Scenario: Re-run after a failure picks up where it stopped
- **WHEN** an operator fixes the bad file and re-runs `scripts/apply_migrations.sh` after a previous failure on 063
- **THEN** the script's to-apply list contains only the migrations from 063 onward (063, 064, 065, ...), it applies them in order, and the previously-applied 054 → 062 are NOT re-run

#### Scenario: --mark-applied records a manually-applied file
- **WHEN** an operator runs `scripts/apply_migrations.sh --mark-applied 070_probe_writes_table.sql manual_backfill`
- **THEN** a single row is inserted into `schema_migrations` with `filename='070_probe_writes_table.sql'`, `source='manual_backfill'`, `checksum=<sha256 of the file>`, and the script exits 0. A subsequent normal run does NOT re-apply 070.

### Requirement: API startup logs schema_migrations ledger status
The Go API's `initDB` (`api/db.go`) SHALL, after the existing `selfTestPool` succeeds, run a check against the `schema_migrations` table and emit exactly one structured log line. The check SHALL use a 5-second context timeout and SHALL NOT crash the API on its own failure.

The log line SHALL be one of:

- `INFO  schema_migrations_check status=present applied=N` — when the table exists and has ≥1 row
- `WARN  schema_migrations_check status=absent hint="run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift"` — when the table does not exist or has 0 rows AND `ZOVARK_REQUIRE_SCHEMA_LEDGER` is unset or `false`
- `ERROR schema_migrations_check status=absent ...` followed by `initDB` returning an error — when the table is absent AND `ZOVARK_REQUIRE_SCHEMA_LEDGER=true`

#### Scenario: Healthy DB with populated ledger
- **WHEN** the API starts against a DB where `scripts/apply_migrations.sh` has been run and `schema_migrations` has 72 rows
- **THEN** the API logs `schema_migrations_check status=present applied=72` at INFO level and continues startup normally

#### Scenario: Drifted dev DB with no ledger, env unset
- **WHEN** the API starts against a fresh dev volume that has only `init.sql` (no `schema_migrations` table) and `ZOVARK_REQUIRE_SCHEMA_LEDGER` is unset
- **THEN** the API logs `schema_migrations_check status=absent hint=...` at WARN level, `initDB` returns nil, and the API binds the listener and serves traffic

#### Scenario: Drifted DB with strict env
- **WHEN** the API starts against the same drifted volume but with `ZOVARK_REQUIRE_SCHEMA_LEDGER=true`
- **THEN** the API logs `schema_migrations_check status=absent ...` at ERROR level, `initDB` returns an error containing the substrings `schema_migrations`, `apply_migrations.sh`, and `RUNBOOK_HEALTHCHECK.md`, and the API process exits non-zero before binding the listener

### Requirement: Operator runbook documents schema-drift symptom and fix
`docs/RUNBOOK_HEALTHCHECK.md` SHALL contain a section anchored as `#schema-drift` titled "POST /api/v1/tasks returns 500 with `42703 column does not exist` (schema drift)" that names:

- The symptom (HTTP 500 with `42703` in the API logs OR a `schema_migrations_check status=absent` warn line at boot)
- The diagnostic command (`scripts/apply_migrations.sh --dry-run`)
- The fix (`scripts/apply_migrations.sh`)
- The strict-mode env var (`ZOVARK_REQUIRE_SCHEMA_LEDGER=true`) and when to set it (production rollouts that should refuse to boot against a drifted DB)
- A cross-reference to the existing `#api-08p01` section (since the prior change was a prerequisite for surfacing this one)

#### Scenario: Operator follows the link from the API warn log
- **WHEN** an operator sees `schema_migrations_check status=absent hint="...RUNBOOK_HEALTHCHECK.md#schema-drift"` in the API logs and opens the runbook at the named anchor
- **THEN** the section contains all five items above and the operator can run the fix without further investigation

## MODIFIED Requirements

### Requirement: e2e probe runs sequential stages with named pass/fail records
`scripts/e2e_probe.sh` SHALL execute its stages in a fixed sequence and SHALL record exactly one `pass`, `fail`, or `skip` outcome per stage to the timeline. The stage sequence SHALL be: `0 login`, `0.5 db_write`, `0.6 schema_ledger`, `1 ingest`, `2 redpanda`, `3 pg.investigating`, `4 pg.completed`, `5 signoz`, `6 verdict`, `7 cleanup`. A failing stage SHALL abort all subsequent stages (they SHALL be recorded as `skip` with detail `prior stage failed`).

#### Scenario: All stages pass on a healthy stack with applied migrations
- **WHEN** the operator runs `scripts/e2e_probe.sh` against a healthy stack where `scripts/apply_migrations.sh` has been run and the ledger is in sync with disk
- **THEN** the timeline contains exactly ten rows (`0 login`, `0.5 db_write`, `0.6 schema_ledger`, `1 ingest`, `2 redpanda`, `3 pg.investigating`, `4 pg.completed`, `5 signoz`, `6 verdict`, `7 cleanup`), each marked `pass`, and the script exits 0

#### Scenario: schema_ledger stage fails — ingest skipped
- **WHEN** the dev volume is drifted (`migrations/061_trace_id.sql` is on disk but missing from `schema_migrations`)
- **THEN** Stage 0.6 records `fail` with detail starting with `drift:` and containing the substring `061_trace_id.sql`, AND Stages 1–7 each record `skip`, AND the script prints a stderr hint pointing at `scripts/apply_migrations.sh` and `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`, AND the script exits non-zero

### Requirement: schema_ledger stage compares on-disk migrations to ledger rows
`scripts/e2e_probe.sh` Stage 0.6 SHALL execute the following comparison and emit `pass` only when the result is empty:

```
on_disk = $(find migrations -maxdepth 1 -name '*.sql' ! -name 'seed_*' -printf '%f\n' | sort)
ledger  = $(docker exec zovark-postgres psql -tAc \
              "SELECT filename FROM schema_migrations ORDER BY filename")
drift   = $(comm -23 <(echo "$on_disk") <(echo "$ledger"))
```

Stage 0.6 SHALL emit `pass` with detail `applied=N drift=0` when `drift` is empty. Stage 0.6 SHALL emit `fail` with detail `drift: <comma-separated filenames, first 5>` when `drift` is non-empty. Stage 0.6 SHALL emit `fail` with detail `ledger absent — run scripts/apply_migrations.sh` when the `psql` query returns SQLSTATE `42P01` (table does not exist).

#### Scenario: Ledger absent
- **WHEN** Stage 0.6 runs against a DB with no `schema_migrations` table
- **THEN** the stage records `fail` with detail starting with `ledger absent` and the script prints a stderr hint of the form `hint: schema_migrations table not found — run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift`

#### Scenario: Single missing migration
- **WHEN** Stage 0.6 runs against a DB where exactly one migration file (`061_trace_id.sql`) is on disk but not in the ledger
- **THEN** the stage records `fail` with detail `drift: 061_trace_id.sql`
