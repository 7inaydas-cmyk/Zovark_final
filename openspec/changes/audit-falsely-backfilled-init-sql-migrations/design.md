## Context

Earlier today, `apply-migrations-054-071-with-ledger` shipped a `schema_migrations` ledger and a static backfill block in `migrations/072_schema_migrations_ledger.sql` declaring all 58 files numbered `000_*` through `053_*` (plus the four duplicate-numbered files at 041 / 050 / 051 / 052) as `source='init_sql'`. That backfill encoded a one-line assumption — *init.sql ≈ migrations 001-053* — that turns out to be partially wrong.

The actual situation, established by a 30-row read-only fingerprint scan against the live dev DB on this branch (2026-04-13):

- Migrations **001–032** are mostly present in the DB, consistent with `init.sql` having been hand-merged through that point. Spot-check pass rate: 9/9 in the 016-030 sample.
- Migrations **033 onwards** are mostly absent, with a handful of exceptions: `035_row_level_security`, `037_performance_indexes`, `039_drop_legacy_tables`, `041_network_beaconing_skill`, `045_investigation_memory`, `050_model_performance_tracking` happen to be present (someone manually applied or hand-merged them at some point). Everything else in the 033-053 range whose fingerprint was checked is missing: 033, 034, 038, 040, 041_system_configs, 042, 043, 044, 046, 047, 048, 049, 051, 052, 053.
- The 28 init_sql rows not yet spot-checked are unknown. Some are in the safe 000-032 range and probably fine; others are in the dangerous 033-053 range and likely also broken. The audit needs to be exhaustive.

The first downstream symptom is the worker failing every workflow at Stage 5 STORE on `column "needs_human_review" of relation "agent_tasks" does not exist` (migration 040). The next failures behind it are latent — every API/worker code path that touches `investigations.fingerprint`, `investigations.dedup_count`, `agent_skills.model_name`, etc. is one query away from the same error.

## Goals / Non-Goals

**Goals:**

- Build a script that, for **every** init_sql-marked row in `schema_migrations`, fingerprints the corresponding migration file against the live DB and reports `present` or `MISSING`. Read-only. Idempotent. Operator-runnable in seconds.
- Build a script that, given the audit output, DELETEs the falsely-marked ledger rows and runs `scripts/apply_migrations.sh` so the runner picks them up and applies them properly with `source='migration_runner'`. `--dry-run` mode that shows the DELETE list without touching the DB.
- Replace `migrations/072_schema_migrations_ledger.sql`'s static `INSERT ... VALUES` backfill block with a fingerprint-conditional one — a `DO $$` block that uses the same fingerprint table as the audit script, so that a fresh `docker compose down -v && up -d` followed by `apply_migrations.sh` produces a ledger that matches the actual DB state, regardless of how stale init.sql is.
- Add `e2e_probe.sh` Stage 0.7 `schema_fingerprint` between Stage 0.6 (`schema_ledger`, filename in ledger) and Stage 1 (ingest). Stage 0.6 catches "filename not in ledger" drift; Stage 0.7 catches "filename in ledger but fingerprint missing" drift. Together they make this entire failure class loud and obvious.
- Apply the audit + repair to the live dev DB **as part of this change**, not as a separate manual step. The dev volume is currently broken; the change must restore it.

**Non-Goals:**

- **Refactoring `init.sql` into per-migration files.** init.sql is a 1500-line hand-merged snapshot; rewriting it is a much larger change with its own audit. Out of scope. This change just makes the ledger honest about what's actually in the DB regardless of init.sql's state.
- **Adopting a third-party migration tool.** Same reasoning as the previous change: bash + psql + the existing ledger is the smallest possible departure.
- **Production rollout.** The repair script will work in prod, but the rollout sequence (drain alerts, `pg_dump`, run audit, run repair, validate, restore on failure) is a separate runbook. Deferred to a follow-up change.
- **Automatic init.sql regeneration.** A future change could auto-regenerate `init.sql` from the current schema state on every release, but that's a much bigger lift. This change just makes the ledger reflect reality.
- **Worker code changes.** The worker fails because the column is missing, not because the worker is wrong. Once the migration runs, the worker keeps working. No `worker/` edits in this change.

## Decisions

### Decision 1: Fingerprint table is a static map embedded in migration 072 and re-used by the audit script

Every migration's fingerprint is one of:

- **table presence**: `to_regclass('public.<table_name>') IS NOT NULL` — for migrations whose primary effect is `CREATE TABLE foo`.
- **column presence**: `EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='foo' AND column_name='bar')` — for migrations that primarily ALTER TABLE … ADD COLUMN.
- **index presence**: `EXISTS(SELECT 1 FROM pg_indexes WHERE indexname='idx_foo')` — for migrations that primarily add an index.
- **constraint presence** (rare): `EXISTS(SELECT 1 FROM pg_constraint WHERE conname='foo_check')` — for migrations whose only effect is to widen a CHECK constraint.

The fingerprint for each migration is encoded as a single `(filename, fingerprint_kind, fingerprint_args)` row in a static SQL `VALUES` table, embedded in `migrations/072_schema_migrations_ledger.sql` AND in `scripts/audit_init_sql_backfill.sh` (or, more maintainably, in a single `migrations/072_fingerprints.sql` snippet that 072 includes via `\i` and the bash script reads via `grep`).

Decision: **encode the fingerprint table as a single `VALUES` block inside `migrations/072_schema_migrations_ledger.sql`**, with a leading comment block describing the format. The audit script reads it by parsing the file (one regex), not by `\i`-ing or by querying the DB. This avoids duplicating the truth across two files and keeps `git blame` honest about who picked which fingerprint for which migration.

The fingerprint table is **hand-curated**, not auto-generated. Why: a migration's "real" effect is often more nuanced than its first DDL statement (e.g., `040_human_review_flags.sql` has both an ADD COLUMN and a CREATE INDEX; the column is the right fingerprint because the index is `WHERE needs_human_review = TRUE` and depends on the column existing). Auto-generating would require a SQL parser and would still need human review per migration.

Estimated effort: 58 fingerprints × ~10 seconds per file = under 10 minutes to populate the table.

### Decision 2: Audit script reads the fingerprint table from migration 072

`scripts/audit_init_sql_backfill.sh`:

1. Reads `migrations/072_schema_migrations_ledger.sql` and extracts the `VALUES` rows from the fingerprint table block (delimited by `-- BEGIN FINGERPRINTS` / `-- END FINGERPRINTS` comment markers).
2. For each row, runs the appropriate `psql -tAc` query against the live DB:
   - `table` → `SELECT to_regclass('public.<arg>') IS NOT NULL`
   - `column` → `SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='<table>' AND column_name='<col>')`
   - `index` → `SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname='<name>')`
3. Prints a per-row result table with `migration | fingerprint_kind | fingerprint_args | present|MISSING` columns and a summary line `audit complete: N present, M missing`.
4. Exits 0 if M=0, 1 if M>0. Read-only, no DB writes.

`--json` flag emits a single JSON object so the repair script and CI can consume it without parsing the table.

### Decision 3: Repair script DELETEs falsely-marked rows and re-runs the runner

`scripts/repair_init_sql_backfill.sh`:

1. Runs the audit internally (or accepts `--from-audit-json <file>` as a passthrough).
2. For each missing migration, prints `would DELETE schema_migrations row for <filename>` (in `--dry-run` mode, exits here) or executes the DELETE.
3. After all DELETEs commit, invokes `scripts/apply_migrations.sh` (no flags) so the runner re-discovers the now-unmarked migrations and applies them with `source='migration_runner'`.
4. Re-runs the audit at the end and asserts 0 missing. Exits 0 on success, 1 on persistent drift.

The DELETE is **per-row** in a single transaction so a partial failure doesn't leave the ledger half-fixed. The runner is invoked AFTER the transaction commits.

Why DELETE rather than UPDATE: the row's `applied_at = '2026-01-01T00:00:00Z'` is a sentinel for "frozen into init.sql before any audit existed". Once the runner actually applies the file, the new row's `applied_at = now()` and `applied_by = 'apply_migrations.sh@…'` records the real apply event. UPDATE would leave the sentinel timestamp in place, lying about when the migration ran. DELETE-then-INSERT-via-runner produces an honest record.

### Decision 4: 072 backfill becomes a `DO $$` block with fingerprint conditionals

The current 072 has 58 hard-coded `INSERT … VALUES (filename, …, 'init_sql', …)` rows. Replace with:

```sql
DO $$
DECLARE
  candidate RECORD;
  fingerprint_present BOOLEAN;
BEGIN
  FOR candidate IN
    SELECT * FROM (VALUES
      ('000_init_agent_tasks.sql',           'table',  'agent_tasks'),
      ('001_sprint1g_entity_graph.sql',      'table',  'entities'),
      ...
      ('053_ioc_evidence_refs.sql',          'column', 'investigations|ioc_evidence_refs')
    ) AS t(filename, kind, args)
  LOOP
    -- Compute fingerprint_present based on candidate.kind / candidate.args
    fingerprint_present := CASE candidate.kind
      WHEN 'table'  THEN to_regclass('public.' || candidate.args) IS NOT NULL
      WHEN 'column' THEN EXISTS(SELECT 1 FROM information_schema.columns
                                 WHERE table_name = split_part(candidate.args, '|', 1)
                                   AND column_name = split_part(candidate.args, '|', 2))
      WHEN 'index'  THEN EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = candidate.args)
    END;

    IF fingerprint_present THEN
      INSERT INTO schema_migrations (filename, applied_at, applied_by, source, checksum)
      VALUES (candidate.filename, '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL)
      ON CONFLICT (filename) DO NOTHING;
    END IF;
  END LOOP;
END $$;
```

A row is only inserted if its fingerprint is observable in the DB at backfill time. Migrations whose fingerprint is missing are simply not inserted — the runner will pick them up on the next pass and apply them properly. This makes the backfill **self-correcting** against any state of `init.sql`, including a future state where init.sql has been further updated or further frozen.

The existing 58-row hard-coded INSERT block is replaced by the `VALUES` rows inside the `FOR candidate IN` loop. The fingerprint table is still hand-curated; the difference is the conditional INSERT.

Why a `DO $$` block instead of a query that joins `VALUES` against `information_schema`: the `EXISTS` predicates use different tables for table / column / index, so a single SELECT-INSERT can't express it cleanly. The `DO $$` is verbose but reads top-to-bottom like documentation.

Why the `args` column uses pipe-delimited `table|column` for column fingerprints: PostgreSQL's `composite type` would be cleaner but adds noise; pipe-split is universally legible.

### Decision 5: Stage 0.7 runs the audit script in `--json` mode, parses the count, and fails on any missing

`scripts/e2e_probe.sh stage07_schema_fingerprint()`:

```bash
local audit_out audit_missing
if ! audit_out=$("$(dirname "$0")/audit_init_sql_backfill.sh" --json 2>&1); then
    record_stage "0.7 schema_fingerprint" "fail" "audit script error: $(echo "$audit_out" | head -c 100)"
    return 1
fi
audit_missing=$(echo "$audit_out" | jq -r '.missing_count // 0')
if [ "$audit_missing" = "0" ]; then
    record_stage "0.7 schema_fingerprint" "pass" "all init_sql fingerprints present"
    return 0
fi
record_stage "0.7 schema_fingerprint" "fail" "missing=$audit_missing — run scripts/repair_init_sql_backfill.sh"
return 1
```

Inserted between `stage06_schema_ledger` and `stage1_ingest` in the runner array. Banner updated. The total e2e probe stage list becomes: 0, 0.5, 0.6, 0.7, 1, 2, 3, 4, 5, 6, 7.

### Decision 6: The repair runs as part of the change's apply, not as a separate manual step

The dev DB is currently broken in ways that block every other change on this branch. So §8 of the task list runs `scripts/audit_init_sql_backfill.sh` against the live dev DB, captures the missing list, runs `scripts/repair_init_sql_backfill.sh`, and re-runs the audit to confirm 0 missing. This is part of the change, not a follow-up.

If the repair surfaces non-idempotency issues with one of the missing migrations (e.g., a `CREATE TABLE` collides with a frozen-in-init.sql table that has a slightly different shape), the change pauses and reports — same protocol as the previous /opsx:apply.

## Risks / Trade-offs

- **[Risk] One of the missing migrations is non-idempotent and fails on a partially-populated DB** → Mitigation: each migration runs in its own transaction inside `apply_migrations.sh`. On failure, the transaction rolls back, the script exits, the ledger row is not inserted, and the operator sees the failing filename. The repair script's final audit re-run will report the persistent drift. Spot-checked the file headers in §1.4 of the previous change; all use `IF NOT EXISTS` patterns. Re-spot-checking is a §2 task.
- **[Risk] A missing migration depends on a column or table that another missing migration is supposed to create, and the ordering matters** → Mitigation: `apply_migrations.sh` processes files in numeric-then-lex order. Any cross-migration dependency in the 033-053 range is automatically respected because the runner walks them in file-name order.
- **[Risk] The v3 governance code path has been running against a DB without `needs_human_review` for an unknown amount of time, and there are workflows in inconsistent state** → Mitigation: query `agent_tasks WHERE status = 'pending' AND created_at > '2026-04-13'` before the repair runs. If there are stuck rows, document them in §8.7. They will be picked up on the next worker poll once the column exists.
- **[Risk] The static `VALUES` block in 072 grows by a row every time a new migration is added** → Mitigation: the previous change's design already accepted this. Each new migration in the 054+ range goes through the runner directly and never touches the init_sql backfill block. The 072 backfill block is **append-only** and **only grows when init.sql is re-frozen** (a separate operator decision). For this change, the block stays at 58 entries — we just make 11+ of them conditional instead of unconditional.
- **[Risk] An operator running the repair on a healthy DB misinterprets the DELETE step as destructive** → Mitigation: the script's `--dry-run` is the documented default in the runbook; the repair section says explicitly *"run --dry-run first; review the DELETE list; only then run for real"*. The DELETEs only touch `schema_migrations` rows, never business tables.
- **[Risk] The fingerprint for a migration is wrong (e.g., the migration creates `foo_v2` but we encoded `foo`)** → Mitigation: the audit's first run is a per-file manual review — the operator looks at every "MISSING" row and confirms the fingerprint matches the file's actual DDL. Wrong fingerprints get fixed at audit time, not at repair time. The §2 task list includes a per-file fingerprint review.
- **[Risk] A future contributor adds a 054+ migration and forgets to add it to the fingerprint table in 072** → Not a risk: 054+ migrations are NOT in the init_sql backfill block at all. They go through the runner the first time they're encountered. The fingerprint table only ever covers the historical 000-053 era.

## Migration Plan

1. Merge to `audit/execution-fixes`.
2. **Read-only audit pass**: `scripts/audit_init_sql_backfill.sh`. Capture the full list of missing migrations.
3. **Manual fingerprint review**: for every "MISSING" row, open the migration file and confirm the encoded fingerprint matches the file's actual DDL. Fix wrong fingerprints in 072 and re-run the audit until the list stabilizes.
4. **`pg_dump` snapshot**: `/tmp/zovark-pre-init-sql-repair-$(date +%Y%m%d-%H%M%S).sql.gz` for rollback safety.
5. **Dry-run repair**: `scripts/repair_init_sql_backfill.sh --dry-run`. Confirm the DELETE list matches the audit's missing list.
6. **Wet repair**: `scripts/repair_init_sql_backfill.sh`. Watch for "DELETED ledger row for NNN" lines, then "applying NNN ..." lines from the chained `apply_migrations.sh` invocation. Final line: `audit complete: <total> present, 0 missing`.
7. **Verify**: `docker compose exec -T postgres psql -U zovark -d zovark -c "\d agent_tasks"` and confirm `needs_human_review` is now present. Restart the worker (`docker compose restart worker`) and watch the next workflow complete past Stage 5 STORE.
8. **Run e2e probe**: `scripts/e2e_probe.sh --signoz-required false`. Stages 0 → 7 should all pass now (Stage 0.7 fingerprint check returns `present`, the worker finishes the workflow, and the probe row reaches a terminal state).
9. **Smoke test**: `scripts/smoke_test_100.sh`. Detection rate must be ≥ baseline.

**Rollback**: each migration runs in its own transaction so a wet-run failure leaves the DB in a partially-fixed state where some of the missing migrations have been applied (with new ledger rows) and some have not. The next runner invocation picks up where the failure happened. Full revert: `gunzip -c /tmp/zovark-pre-init-sql-repair-*.sql.gz | docker compose exec -T postgres psql -U zovark -d zovark` (which restores the entire DB to the pre-repair snapshot, including the false ledger rows). The change document records both the snapshot path and the rollback command.

## Open Questions

- **Should the repair script also DROP and re-CREATE schema objects that exist in init.sql but with the wrong shape?** Out of scope here. Such mismatches would surface as `CREATE TABLE foo` errors during the wet run and are tracked as their own follow-up. The current change assumes the mismatches are limited to *missing* objects, not *wrong-shape* objects. If we find any wrong-shape mismatches, the change pauses.
- **Should the fingerprint table cover migrations 054+ as well, for a uniform audit story?** No. 054+ are applied by the runner with `source='migration_runner'` and have a real `applied_at` timestamp; the runner's idempotency check is sufficient for them. The fingerprint table exists only for the init_sql era where "applied" and "in the DB" can disagree.
- **Should `migrations/072_schema_migrations_ledger.sql` be renamed to reflect its dual purpose (ledger + fingerprint backfill)?** Rejected — renaming the file would break the ledger row that already references it (`filename = '072_schema_migrations_ledger.sql'`). The leading comment block and the `DO $$` block document the dual purpose.
