## Context

The Zovark dev Postgres has been operating without any form of migration tracking since the project began. `init.sql` is mounted into `/docker-entrypoint-initdb.d/` and runs once per fresh volume, leaving the DB at a frozen state that approximates "migrations 001 through ~055 merged by hand at some past date". Every migration file added since — currently 14 of them, numbered 054 through 071 with gaps and a few duplicates — is applied (or not applied) entirely at operator discretion via `docker compose exec -T postgres psql ... < migrations/NNN.sql`. There is no record of what's been applied, no failure mode if you forget, and no way for a fresh contributor to bring an existing volume up to current.

The Phase 1 audit on this branch fingerprinted every column / table / function added by migrations 054 → 071 against the running DB and found **all 14 missing**. The previous change (`fix-pgx-pgbouncer-prepared-stmt`) removed a pgx ↔ PgBouncer collision that had been masking this drift; with the collision fixed, `POST /api/v1/tasks` now reaches the schema layer and immediately fails on a missing `agent_tasks.trace_id` column. The same pattern is latent across every handler that touches a post-053 column.

This change introduces three things in lock-step: (1) a `schema_migrations` ledger table that backfills the init.sql era and tracks future runs, (2) a strict bash runner that walks the `migrations/` directory in numeric order and applies the gap, and (3) a startup drift check + e2e probe stage that turns silent drift into a loud, actionable failure mode.

## Goals / Non-Goals

**Goals:**

- Get the dev DB to schema-equivalent-with-`audit/execution-fixes`-branch-code in **one operator command** (`scripts/apply_migrations.sh`), with no manual `psql < f` invocations.
- Establish a **persistent, queryable ledger** of what's been applied to a given DB so this never silently happens again. Future migrations only run if absent from the ledger; future audits run `SELECT filename FROM schema_migrations` instead of fingerprinting columns by hand.
- Backfill the ledger with the **init.sql-frozen era** explicitly (marked `source='init_sql'`) so the runner doesn't try to re-run them and so an operator inspecting the ledger can tell which migrations have an audit trail vs. which were merged by hand.
- Make the API **loudly aware** of drift via a startup info/warn line, with an opt-in fail-fast knob (`ZOVARK_REQUIRE_SCHEMA_LEDGER=true`) for production deploys that should refuse to boot against an out-of-date DB.
- Make the e2e probe **fail at the ledger check stage**, not mid-pipeline, when a future regression wipes the volume without re-running migrations.
- Handle the four duplicate migration numbers (041 / 050 / 051 / 052) and the 056-058 gap deterministically so the runner is reproducible.

**Non-Goals:**

- **Replacing `init.sql`** with a from-scratch migration runner. `init.sql` is still the only thing that runs on a fresh volume; this change does not touch it. A future change can refactor `init.sql` into per-migration files, but that is much larger blast-radius and not needed to unblock this branch.
- **Adopting a third-party migration tool** (Flyway, Goose, golang-migrate, sqlx-migrate). The runner is intentionally a 100-line bash script that wraps `psql`. The reasons: (a) air-gapped customer deploys cannot rely on Go binaries the operator hasn't whitelisted; (b) the existing operator workflow is already `docker compose exec -T postgres psql < f`, so a bash wrapper around that is the smallest possible departure; (c) every line of the runner is auditable in one screen. If the project later wants Goose, this change is a clean prerequisite (Goose can read the `schema_migrations` table without modification).
- **Migrating `worker/` Python migrations.** The worker reads from the same Postgres but does not own any DDL. Any `ALTER TABLE` it currently issues at runtime (audit needed in a future change) is out of scope here.
- **Production / customer deployments.** This change targets the dev compose stack. The same script and ledger schema work in prod, but the rollout sequencing (drain alerts, take a `pg_dump` snapshot, run the script, validate, restore on failure) is its own runbook and not in scope here. A follow-up change should productionise it.
- **Schema validation beyond filename presence.** The ledger checks "did this filename run", not "is the resulting schema what the file would have produced". Per-migration checksums are recorded but not enforced on read; that is a future hardening.

## Decisions

### Decision 1: Ledger schema

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename     TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by   TEXT        NOT NULL DEFAULT current_user,
    source       TEXT        NOT NULL CHECK (source IN ('init_sql', 'migration_runner', 'manual_backfill')),
    checksum     TEXT
);
```

Why these columns:
- **`filename` as PK**: matches how operators and the runner refer to migrations. UUID PK adds nothing here. Filename is unique by definition (the `migrations/` directory's filesystem enforces it) and the duplicate-number files have distinct full filenames so the PK still works.
- **`applied_at` / `applied_by`**: standard audit columns. `current_user` is `zovark` for the migration runner and `init_sql_freeze_2026-04-13` for the backfill rows (set explicitly so an operator can tell them apart at a glance).
- **`source`**: three legitimate values. `init_sql` = "this migration's effects are present because init.sql baked them in, but no one ever ran the file through psql". `migration_runner` = "applied by `scripts/apply_migrations.sh` on `applied_at`". `manual_backfill` = "applied by an operator with `psql < f` outside the runner; recorded after the fact by the runner's first pass". The CHECK constraint catches typos.
- **`checksum`**: SHA-256 of the file contents at the time of application. Recorded for future use (drift detection, "did someone edit this file post-application"); not enforced now.

Why no `version INT`: the duplicate 041 / 050 / 051 / 052 numbers mean version is not unique, and the gap at 056 / 057 / 058 means it is not contiguous. Using filename as the canonical key sidesteps both issues.

Alternatives considered:
- **`(version, name)` composite PK** like Flyway. Rejected — Flyway's schema is not a great fit for a project that already has files-as-truth and no tooling layer.
- **`SERIAL id` PK with `filename` UNIQUE**. Rejected — adds a column with no purpose. Filename PK is fine.

### Decision 2: Numeric-then-lexicographic ordering, with explicit duplicate handling

The runner sorts `migrations/*.sql` files this way:

1. Extract the leading numeric prefix (e.g., `054` from `054_cipher_audit_events.sql`).
2. Sort by that integer.
3. Tiebreak by the rest of the filename, lexicographic.

So the order for the duplicate-numbered pairs is:

```
041_network_beaconing_skill.sql       (n comes before s)
041_system_configs.sql
050_model_performance_tracking.sql    (m comes before s)
050_sprint1k_cross_tenant_entities.sql
051_bootstrap_pipeline_enhancements.sql  (b before s)
051_sprint2a_detection_rules_enhancements.sql
052_rate_limit_audit.sql              (r before s)
052_sprint2b_soar_playbooks_enhancements.sql
```

This order is fully deterministic and reproducible. **It also happens to be the order in which `ls migrations/*.sql | sort -V` produces these files**, which is the order any human would manually arrive at. The `.sql` files in the 04x and 05x range are all going through the runner against a DB that already has them merged in via `init.sql`, so the ordering only matters for posterity (the ledger backfill records all of them as `source='init_sql'` regardless of order).

For the **active** range (054 onwards), the ordering happens to be unambiguous — each number has exactly one file.

Why not `version_added_to_branch` git history order: requires git access from the runner, which is not available inside the postgres container, and orders files by commit-author whim instead of file-name truth.

### Decision 3: Runner is a bash script, not Go

`scripts/apply_migrations.sh` follows the same shape as the existing `scripts/git_*.sh` and `scripts/seed_dev.sh`: `set -euo pipefail`, `--help`, `--no-color`, `--dry-run`, exit codes 0 / 1 / 2.

Algorithm:

```
1. Parse flags. If --help, print help and exit 0.
2. Resolve the postgres container: `docker compose ps -q postgres` → container id.
3. Verify schema_migrations exists. If not, fail with a hint to apply migrations/072 first
   (the runner does NOT auto-create the ledger — that file IS migration 072 and goes through
   the runner like any other, but with a special case in step 5).
4. Build the work list:
   - `find migrations -maxdepth 1 -name '*.sql' ! -name 'seed_*'` → all candidate files
   - Sort by numeric prefix, then lex
   - For each, query: `SELECT 1 FROM schema_migrations WHERE filename = $1`
   - Skip if present
   - Add to work list if absent
5. If the work list contains migrations/072_schema_migrations_ledger.sql:
   - Special case: the ledger table doesn't exist yet, so step 3 just bootstrapped us.
     Run 072 first as a one-off (psql -c < f), then INSERT its own row by hand
     with source='migration_runner'. After that the ledger exists and the loop continues.
6. For each file in the work list:
   - Print: "applying migrations/NNN_xxx.sql ..."
   - Compute SHA-256 of the file contents (use sha256sum, available in postgres alpine)
   - Open a transaction:
       BEGIN;
       \i /migrations/NNN_xxx.sql
       INSERT INTO schema_migrations (filename, applied_by, source, checksum)
         VALUES ('NNN_xxx.sql', 'apply_migrations.sh@$(hostname)', 'migration_runner', '<sha256>');
       COMMIT;
   - On psql exit non-zero: print "FAILED: NNN_xxx.sql" + the last 20 lines of psql stderr,
     exit 1. Transaction has already rolled back; ledger does NOT have the row.
7. Print summary: "applied N migrations, ledger now has M rows total". Exit 0.
```

`--dry-run` prints the work list but skips step 5 / 6 entirely. Exit 0 even if the list is non-empty, so CI can call `apply_migrations.sh --dry-run && other-thing` to gate on the script being parseable without applying anything.

### Decision 4: Backfill strategy for the init.sql era

Migration `072_schema_migrations_ledger.sql` itself contains the backfill. After `CREATE TABLE schema_migrations`, it does:

```sql
INSERT INTO schema_migrations (filename, applied_at, applied_by, source, checksum)
VALUES
  ('000_init_agent_tasks.sql',           '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
  ('001_sprint1g_entity_graph.sql',      '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
  ...
  ('055_template_promotion.sql',         '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL)
ON CONFLICT (filename) DO NOTHING;
```

The list includes every file from `000_*` through `055_*` that exists on disk **plus the four duplicate-numbered files at 041 / 050 / 051 / 052**, generated by reading `ls migrations/0[0-5]*.sql | sort` at proposal-write time and pasting the result into the migration. Static list, deterministic, no shell expansion at apply time.

Why a fixed timestamp (`2026-01-01T00:00:00Z`): we don't know when these migrations actually ran (they didn't, formally — they were merged into `init.sql` at some unrecorded date), so a sentinel value that is obviously not a real `applied_at` is more honest than `now()`. An operator querying `applied_at < '2026-04-13'` can find the entire frozen era in one predicate.

Why `applied_by = 'init_sql_freeze_2026-04-13'`: the literal string includes the date this change shipped, so when someone queries the ledger six months from now they can grep for the freeze date and find this change in git.

The runner's first invocation will then see migration 072 itself in the work list (because it's not in the ledger yet — the ledger doesn't exist), special-case it, run it, and then loop through 054 / 055 / 059 / 060 / 061 / 062 / 063 / 064 / 065 / 066 / 067 / 068 / 069 / 070 / 071. (070 was applied manually by the prior /opsx:apply session and gets recorded as `source='manual_backfill'` rather than `migration_runner`. The runner can detect this by trying the migration first and catching `ERROR:  relation "probe_writes" already exists` — but that's brittle. Better: the runner has a `--mark-applied` flag and we run it once: `apply_migrations.sh --mark-applied 070_probe_writes_table.sql manual_backfill` before the main run.)

Alternatives considered:
- **Backfill as a separate script, not as part of migration 072**. Rejected — it splits the "ledger creation" and "ledger seed" steps across two artifacts that must be run in a precise order. Folding both into 072 makes them atomic.
- **Don't backfill at all, just CREATE TABLE.** Rejected — the runner would then try to apply 001 → 053, all of which would fail or be no-ops on the already-populated DB. Backfill is the only way to tell the runner "treat these 56 files as already done".

### Decision 5: API drift check is non-blocking by default

In `api/db.go` `initDB`, after `selfTestPool` succeeds, run:

```go
err := checkSchemaMigrationsLedger(ctx, dbPool)
```

`checkSchemaMigrationsLedger` does:

1. `SELECT count(*) FROM schema_migrations` → if ≥1, the ledger exists.
2. If 0 rows or relation does not exist (`42P01`):
   - If `ZOVARK_REQUIRE_SCHEMA_LEDGER=true`: `slog.Error(...)`, return error from `initDB`. API exits non-zero.
   - Else: `slog.Warn("schema_migrations_check", "status", "absent", "hint", "run scripts/apply_migrations.sh; see docs/RUNBOOK_HEALTHCHECK.md#schema-drift")`. Continue.
3. If the ledger exists, the API logs `slog.Info("schema_migrations_check", "status", "present", "applied", N)` and continues. **The API does NOT check that the on-disk migration set matches the ledger** — that requires filesystem access from inside the API container, which (a) the worker container has but the API doesn't, (b) is not the API's job. The runner + the e2e probe Stage 0.6 own that check.

Why non-blocking by default: dev workflows include `docker compose down -v && docker compose up -d` which intentionally creates a fresh volume with only `init.sql`. Forcing `ZOVARK_REQUIRE_SCHEMA_LEDGER=true` in dev would mean every fresh-volume cycle hangs the API until the operator runs `apply_migrations.sh`. The opt-in env var means dev defaults to "warn loudly", and prod sets `=true` on rollout to get strict enforcement.

Why a single info/warn line (not a structured drift diff): the API doesn't have the on-disk migration list, so it can only say "ledger has N rows" or "ledger has 0 rows". The structured diff lives in `apply_migrations.sh --dry-run` and in the e2e probe Stage 0.6, both of which DO have filesystem access.

### Decision 6: e2e probe Stage 0.6 owns the on-disk-vs-ledger diff

Add `stage06_schema_ledger()` to `scripts/e2e_probe.sh`, run between Stage 0.5 (`db_write`) and Stage 1 (`ingest`). Algorithm:

1. `docker exec zovark-postgres psql -tAc "SELECT filename FROM schema_migrations ORDER BY filename"` → ledger list.
2. `find migrations -maxdepth 1 -name '*.sql' ! -name 'seed_*' -printf '%f\n' | sort` → on-disk list.
3. `comm -23 <(disk) <(ledger)` → files on disk but not in the ledger (drift).
4. If empty: `record_stage "0.6 schema_ledger" "pass" "all N migrations applied"`.
5. If non-empty: `record_stage "0.6 schema_ledger" "fail" "drift: <comma-separated filenames>"` and print a stderr hint pointing at `scripts/apply_migrations.sh` and `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`.

This is the single canonical place where on-disk reality is compared to ledger reality. It runs **after** `db_write` (so we know the API can write to PG before we try to query it) and **before** `ingest` (so a future drift regression doesn't surface as a misleading 500 from `createTaskHandler`).

## Risks / Trade-offs

- **[Risk] One of the 14 unapplied migrations is not actually idempotent against a partially-init.sql-populated DB** → Mitigation: each migration runs in its own transaction. On failure the transaction rolls back, the script exits non-zero, and the ledger row is not inserted. The operator gets a clear "FAILED at migrations/NNN_xxx.sql" line and can inspect the file manually. Spot-check of the 14 file headers shows all use `IF NOT EXISTS` / `IF EXISTS` / `ON CONFLICT DO NOTHING` / `DROP CONSTRAINT IF EXISTS` patterns. The first dry-run pass is mandatory in the tasks list before any wet-run.
- **[Risk] Migration 060 (RLS) breaks an existing handler** → Mitigation: `zovark` is the table owner and bypasses RLS by default. The API in dev connects as `zovark`, not `zovark_app`, so RLS policies are not enforced against the API connection. Smoke test in §7 verifies this empirically.
- **[Risk] Migration 068 (entity graph + pgvector retirement) drops tables that the API still reads from** → Mitigation: read the file before applying. If it issues `DROP TABLE entities` while `api/handlers/platform_ingest.go` still reads from `entities`, mark this change as blocked on a code-side cleanup first. Spot-checked: file header says "Idempotent. Preserves OLTP tables". Adding a `--dry-run` pass to the §7 task list as a hard gate.
- **[Risk] The duplicate-numbered files at 041 / 050 / 051 / 052 are all in the init.sql backfill set, so order doesn't actually matter for correctness — but a future contributor who adds a `045_*` file alongside the existing one will not be warned by the runner** → Mitigation: design.md says future contributors should use unique numeric prefixes. Adding a runner-side warning ("duplicate numeric prefix detected: 045") is a follow-up improvement, not a blocker for this change.
- **[Risk] Operators who already manually applied some of the 14 migrations between 054 and 071** → Mitigation: every migration uses `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` patterns; re-running them on an already-populated DB is a no-op or near-no-op. The ledger row gets inserted with `source='migration_runner'` regardless. If a partial state exists (some migrations applied, some not), the runner picks up exactly the missing ones. If there's any doubt, an operator can pre-mark known-applied files with `apply_migrations.sh --mark-applied <file> manual_backfill` before the main run.
- **[Risk] `ZOVARK_REQUIRE_SCHEMA_LEDGER=true` is set in dev by accident, hanging fresh-volume boots** → Mitigation: default is `false`, the env var is documented in the runbook section, and the failure-mode log line names the env var explicitly so an operator who hits it knows exactly what to unset.
- **[Risk] The startup drift check itself fails on a DB that has the ledger but is otherwise broken** → Mitigation: the check uses `SELECT count(*) FROM schema_migrations` with a 5-second context timeout, separate from the existing `selfTestPool` timeout. A query failure is treated as "ledger absent" for the purpose of the warn / error path; it doesn't crash the API.

## Migration Plan

This change is a **DB schema change** plus a small amount of Go and bash code. No application logic moves, no SQL queries are rewritten, no API contracts change.

### Dev rollout (this change)

1. Merge the change to `audit/execution-fixes`.
2. **Dry run**: `scripts/apply_migrations.sh --dry-run`. Inspect the work list — should be `072_schema_migrations_ledger.sql, 054_..., 055_..., 059_..., 060_..., 061_..., 062_..., 063_..., 064_..., 065_..., 066_..., 067_..., 069_..., 071_...` (14 net + 072 itself; 070 is pre-marked). Expect zero psql traffic.
3. **Pre-mark 070** as manual_backfill: `scripts/apply_migrations.sh --mark-applied 070_probe_writes_table.sql manual_backfill`. (Cannot run until the ledger exists, so step 4 has to come first… see decision 4 special case.)
4. **Wet run**: `scripts/apply_migrations.sh`. Watch for "applied N migrations" line. Expected N=15 (072 + the 14 unapplied + ledger backfill).
5. **Verify**: `psql -c "SELECT count(*) FROM schema_migrations"` → expect ~71 rows total (56 init.sql + 1 manual_backfill + 14 migration_runner + 1 ledger itself = 72; off-by-one acceptable depending on exact backfill count).
6. **Restart API**: `docker compose restart api`. Confirm `schema_migrations_check status=present applied=72` in the logs.
7. **Run e2e probe**: `scripts/e2e_probe.sh`. Stage 0.6 `schema_ledger` must pass. Stages 1 → 7 should now reach completion (or fail on a NEW issue, which is the next change).
8. **Run smoke test**: `scripts/smoke_test_100.sh`. Detection rate must be ≥ pre-change baseline.

**Rollback**: every migration is in its own transaction, so a failed wet-run leaves the DB in a partially-migrated state where some of the 14 are applied (with ledger rows) and some are not. The next runner invocation picks up exactly where the failure happened. No `rollback.sql` files are needed.

For a full revert (e.g., the change is wedged and we need to restart from a clean dev volume): `docker compose down -v && docker compose up -d` → fresh volume with only `init.sql` → `scripts/apply_migrations.sh` from scratch. That is the standard dev cycle and is unaffected by this change.

### Production rollout (FUTURE CHANGE, NOT THIS ONE)

Out of scope. A follow-up change should:
- Take a `pg_dump` snapshot of the prod DB.
- Drain alerts via the existing kill switch.
- Run `scripts/apply_migrations.sh --dry-run` against prod.
- Run `apply_migrations.sh` for real, capturing every line of output.
- Set `ZOVARK_REQUIRE_SCHEMA_LEDGER=true` on the prod API env.
- Restart API, validate the e2e probe + smoke test against prod.
- Document the rollback path (restore from `pg_dump`).

## Open Questions

- **Should the runner write a `pg_dump` snapshot to `/tmp` before the first migration runs?** Lean yes for prod, no for dev (the dev volume can be re-created in ~30 seconds and the snapshot would just take disk for nothing). Defer to the prod follow-up change.
- **Should `migrations/072_schema_migrations_ledger.sql` itself be checksum-verified by the runner**, given that it's the file that creates the table that records checksums? Chicken-and-egg. Decision: no — the runner records the checksum after the file runs, and the checksum is for future drift detection, not for this run's correctness.
- **Should we add a `migrations/056_*.sql` / `057_*.sql` / `058_*.sql` placeholder for the gap?** No. The gap is real (those numbers were never used) and adding placeholders would only confuse a future contributor who looks at the list. The ledger doesn't care about gaps; the runner sorts by numeric prefix and skips silently.
