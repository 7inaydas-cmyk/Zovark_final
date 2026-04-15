## Why

The Phase 1 audit (this branch, 2026-04-13) confirmed that the dev Postgres volume has **no migration ledger** (`schema_migrations` table does not exist) and that **14 migration files in `migrations/054_*.sql` through `migrations/071_*.sql` were never applied**. The DB is the literal output of `init.sql` — 81 tables, frozen at roughly the migration-053 era — and nothing else.

The hard symptom is `POST /api/v1/tasks` returning HTTP 500 with `[ERROR] create task record: ERROR: column "trace_id" of relation "agent_tasks" does not exist (SQLSTATE 42703)`. That column is added by `migrations/061_trace_id.sql` (Mission 4: Global Request Tracing). The full set of unapplied migrations the API now references at handler-write time is:

| Mig | Adds | Used by |
|---|---|---|
| 054_cipher_audit_events | `cipher_audit_events` table | `api/cipher_audit_handlers.go` |
| 055_template_promotion | `template_promotion` table | template promotion flywheel |
| 059_template_promotion_quorum | `template_promotion_approvals` | `api/promotion_handlers.go` |
| 060_row_level_security | RLS policies on 10 tables | `api/db.go` `beginTenantTx` (defence-in-depth) |
| **061_trace_id** | `agent_tasks.trace_id`, `audit_events.trace_id` | **`api/task_handlers.go createTaskHandler` (this is the current 500)** |
| 062_v3_tool_calling | `governance_config`, `institutional_knowledge`, `agent_tasks.{path_taken, plan_executed, execution_mode}` | v3 governance pipeline + `worker/stages/govern.py` + `worker/stages/store.py` |
| 063_system_tenant | `tenants` row id `…0001` | break-glass auth |
| 064_dedup_count | `agent_tasks.dedup_count` | `api/alert_dedup.go` v2 dedup writeback |
| 065_ocsf_ingest | OCSF schema columns + `agent_tasks.dedup_hash` index | `api/handlers/platform_ingest.go` |
| 066_detection_candidate_validation_failed | `detection_candidates.status` enum widening | detection rule generator |
| 067_audit_platform_governance_events | `audit_events.event_type` enum widening | governance + platform audit handlers |
| 068_ticket2_surreal_graph_pgvector_retirement | retire pgvector entity tables | OLTP cleanup |
| 069_ticket4_mcp_keys_sessions_audit | `mcp_api_keys`, `mcp_sessions`, governance audit type | `api/main.go` MCP routes |
| 071_partition_maintenance | partition maintenance functions for `audit_events` / `investigations` beyond 2026-12 | future-dated audit writes |

(Migration 070 — `probe_writes_table.sql` — was applied manually during the previous /opsx:apply session and is the only `migrations/*.sql` file currently reflected in the DB. There are no files at numbers 056 / 057 / 058.)

### Why this happened (and why it will keep happening)

1. **No migration ledger.** Postgres has no record of which migrations have run. There is no `schema_migrations` table, no `flyway_*` table, no version stamp anywhere. The DB cannot tell you what's been applied. Any audit has to fingerprint each migration manually.
2. **No migration runner in the boot path.** `docker-compose.yml` mounts `init.sql` and `migrations/seed_dev_data.sql` into `/docker-entrypoint-initdb.d/`, which Postgres runs **once** on first volume init and never again. Subsequent migration files under `migrations/*.sql` are operator-applied with `docker compose exec -T postgres psql -U zovark -d zovark < migrations/NNN.sql` — entirely manual, with no enforcement.
3. **`init.sql` is a hand-merged frozen snapshot.** It contains roughly the schema state of migrations 001–053 (plus a few tables like `entity_observations` that don't exist as separate migration files). It has no version stamp, no `MERGED_THROUGH = N` comment. Anyone who reads `init.sql` cannot tell what era it represents.
4. **Duplicate migration numbers** (4 collisions at 041 / 050 / 051 / 052) and a **gap** at 056 / 057 / 058 mean any file-name-only sort produces an ambiguous order. A future migration runner needs a deterministic tiebreak.
5. **Audit/execution-fixes branch silently assumed the migrations were applied.** `audit/execution-fixes` (the current branch) added new code that references `agent_tasks.trace_id`, `dedup_count`, the v3 governance tables, etc. without verifying the schema was at the matching version. Every commit on this branch is one fresh `docker compose down -v && docker compose up -d` away from being broken.

The previous change (`fix-pgx-pgbouncer-prepared-stmt`) successfully removed the pgx ↔ PgBouncer collision that was masking this drift. With that fix in place the e2e probe Stage 0.5 `db_write` passes in ~1ms and Stage 1 `ingest` is the next thing to fail — exactly because it now reaches the schema layer and finds a missing column. This change is the natural follow-up.

## What Changes

- **Add a `schema_migrations` ledger table** to the dev Postgres via a new `migrations/072_schema_migrations_ledger.sql`. Schema: `(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), applied_by TEXT NOT NULL DEFAULT current_user, source TEXT NOT NULL CHECK (source IN ('init_sql', 'migration_runner', 'manual_backfill')), checksum TEXT)`. The `source` column distinguishes migrations that were frozen into `init.sql` from those run by the new migration runner — critical for audit on a stack that has no other ledger.
- **Backfill the ledger** in the same migration with all migration filenames from `000_*.sql` through `053_ioc_evidence_refs.sql` plus the four duplicate-numbered files at 041 / 050 / 051 / 052 (58 rows total), marked `source='init_sql'`, `applied_at='2026-01-01T00:00:00Z'`, `applied_by='init_sql_freeze_2026-04-13'`. This declares the init.sql-frozen era explicitly so the migration runner skips them on subsequent runs. **Migrations 054 and 055 are NOT in the backfill** — the Phase 1 audit confirmed both `cipher_audit_events` and `template_promotion` are absent from the dev DB, so init.sql does not bake them in. Both are applied by the runner alongside 059–071.
- **Add `scripts/apply_migrations.sh`** — a strict bash script (`set -euo pipefail`, `--help`, `--dry-run`, `--no-color`) that walks `migrations/*.sql` in **numeric-then-lexicographic** order, looks up each filename in `schema_migrations`, and for any unapplied file: opens a transaction, runs the file via `psql`, inserts the ledger row with `source='migration_runner'`, and commits. On any `psql` non-zero exit the transaction rolls back, the script exits non-zero, and a clear "FAILED at migrations/NNN_xxx.sql line N" line is printed.
- **Apply the 14 unapplied migrations** (054, 055, 059, 060, 061, 062, 063, 064, 065, 066, 067, 068, 069, 071) to the dev DB through the new script. `070_probe_writes_table.sql` is already in the DB but **not yet** in the ledger; the script's first run also inserts it as `source='manual_backfill'`.
- **Add a startup drift check to the API** (`api/db.go` `initDB`, after the existing `selfTestPool`) that runs a single `SELECT count(*) FROM migrations_on_disk_view EXCEPT SELECT filename FROM schema_migrations` (or the equivalent expressed without a view — see design.md) and emits one of:
  - `slog.Info("schema_migrations_check", "status", "in_sync", "applied", N)` — happy path
  - `slog.Warn("schema_migrations_check", "status", "drift", "missing", [filenames])` — degraded but boots
  - `slog.Error(...)` + `return err` from `initDB` if `ZOVARK_REQUIRE_SCHEMA_LEDGER=true` (default `false`) — opt-in fail-fast for production
- **Update `docs/RUNBOOK_HEALTHCHECK.md`** with a new section anchored `#schema-drift` documenting the symptom (HTTP 500 from `POST /api/v1/tasks` with `42703 column does not exist`), the diagnostic command (`scripts/apply_migrations.sh --dry-run`), and the fix (`scripts/apply_migrations.sh`).
- **Add a Stage 0.6 `schema_ledger` to `scripts/e2e_probe.sh`** between Stage 0.5 (`db_write`) and Stage 1 (`ingest`) that fails the probe if the ledger reports drift, with a clear hint pointing at the runbook anchor and the script. Catches a future regression where someone wipes the dev volume but forgets to re-run migrations.
- **Cross-reference from `CLAUDE.md`** — extend the existing Known Issue #12 (added by the pgx fix) with a #13 entry pointing at the schema-drift runbook section, and update the `## Database` section to mention the new ledger and the `apply_migrations.sh` workflow.

## Capabilities

### New Capabilities

- `schema-migration-ledger`: a tracked, queryable, fail-fast-able view of which `migrations/*.sql` files have been applied to a given Zovark Postgres database. Owns: the `schema_migrations` table, the `apply_migrations.sh` runner, the API startup drift check, and the e2e-probe Stage 0.6.

### Modified Capabilities

- `e2e-pipeline-probe`: gains a Stage 0.6 `schema_ledger` between `db_write` and `ingest`. A future drift regression is reported as a ledger fault, not as a misleading "ingest failure" or "missing column" mid-pipeline.

## Impact

- **Affected code**: new `migrations/072_schema_migrations_ledger.sql`, new `scripts/apply_migrations.sh`, edits to `api/db.go` (drift check + new env var), edits to `scripts/e2e_probe.sh` (Stage 0.6 + stage runner array), edits to `docs/RUNBOOK_HEALTHCHECK.md` (new section + cross-link from `#api-08p01` section), edits to `CLAUDE.md` (Known Issue #13 + Database section).
- **Affected DB state**: on the dev volume, this change adds the ledger table, backfills 56 init.sql-era rows, applies 14 migration files (054–069 sans 056/057/058, plus 071), and inserts 15 ledger rows for them (the 14 newly applied + 1 backfill of 070). Net change: ~14 new tables/columns/policies, 71 ledger rows, 0 data rows touched in business tables.
- **Risk**: medium-high on the **first** run, low on every subsequent run.
  - First-run risk vectors: (a) one of the 14 migrations is not actually idempotent and fails on the partially-init.sql-populated DB (mitigation: each migration runs in its own transaction so a failure rolls back cleanly and the script exits — no partial migration state is committed); (b) one of the 14 migrations references a table or column that depends on a migration in the 056 / 057 / 058 gap (mitigation: spot-checked the file headers — all present migrations look self-contained or `IF NOT EXISTS`-guarded; the dry-run pass will flag any `pg_dump` issues); (c) RLS migration 060 silently breaks an existing handler that didn't go through `beginTenantTx` (mitigation: `zovark` user is the table owner and RLS-bypassing by default — RLS only enforces against `zovark_app`, which is not used by the API in dev; the smoke test in §7 catches any handler regression anyway).
  - Steady-state risk: zero. The ledger is read-only after the first run unless someone adds a new migration, and the runner is `set -euo pipefail` strict.
- **Breaking**: none for application code. The API gains a new env var (`ZOVARK_REQUIRE_SCHEMA_LEDGER`, default `false`) and a new info/warn log line; existing behaviour is unchanged. The new e2e probe stage runs before existing stages, so any old probe scripts with hand-rolled stage assertions are unaffected.
- **Followup hooks**: customer/prod deployments will need the same `apply_migrations.sh` run before the next API version that depends on these columns can boot. The `ZOVARK_REQUIRE_SCHEMA_LEDGER=true` env var is the production knob — set it on customer deploys after the runner has been validated against their DB volume.
