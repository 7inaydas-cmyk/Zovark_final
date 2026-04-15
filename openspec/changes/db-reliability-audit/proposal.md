## Why

Before Zovark v3.2.1 ships to a customer environment, we need a documented answer to "can PostgreSQL hold up at 2 a.m. on a Saturday with nobody watching?" The audit pass uncovered eight categories of latent issues that say no:

1. **Single point of failure** — the API hard-fails to start without PgBouncer (`api/main.go:158`, compose `depends_on: pgbouncer:service_healthy`), but the worker silently falls back to direct Postgres on a different password. PgBouncer dying produces inconsistent behaviour across services.
2. **Temporal shares the same Postgres** as application data (`docker-compose.yml:224-249`, `POSTGRES_SEEDS=postgres`) and connects direct on `:5432`, bypassing PgBouncer's `MAX_DB_CONNECTIONS=50` cap. A runaway app query can starve Temporal's workflow polling.
3. **Migration runner has duplicate numbering** — `041`, `050`, `051`, `052` each have two files (one Apr 10, one Apr 12). The bash runner sorts lexicographically and applies them in non-deterministic order; it never inserts into `schema_migrations`, so re-runs are fragile.
4. **Backup is documented but unscheduled** — `scripts/backup-db.sh` works, but no cron / systemd timer / k8s CronJob fires it. Only `postgres_data` gets dumped; `redpanda_data` (task queue), `surreal_data` (entity graph copy), and Temporal's own state inside Postgres are never independently captured.
5. **Resource exhaustion is invisible** — `data_retention_policies` table exists (migration 013) but no job ever cleans up, no Prometheus alert fires on disk, WAL backlog, or database growth, and Temporal's visibility tables grow unbounded.
6. **PgBouncer failure modes are split** — API depends on PgBouncer healthy; worker doesn't. PgBouncer healthcheck only probes its own loopback, not the upstream Postgres.
7. **Hardcoded credentials everywhere** — 40+ worker modules hardcode `zovark_dev_2026` (the *wrong* DB password) as `os.environ.get` defaults bypassing `worker/settings.py`. `worker/stages/store.py:27` hardcodes `zovark-redis-dev-2026` (the *wrong* Redis password) — dedup writebacks have been silently failing in dev for months. `TestPass2026` lives in 4 production-path files (`agent/healer.py:81`, `cmd/zvadmin/queue.go:125`, `sdk/python/zovark/client.py:9`, `dpo/dpo_forge.py:471`). `helm/zovarc/values.yaml:134` ships the wrong password.
8. **SurrealDB blast radius is uncatalogued** — 11 functions in `worker/surreal_graph.py`, 8 importers, 1 compose service, 6 env vars, 1 volume, 1 migration. Need a single removal checklist before the entity-graph fix lands.

## What Changes

- **Audit deliverable**: produce `docs/DATABASE_RELIABILITY_AUDIT.md` containing the eight findings groups above, every fact backed by `file:line`, severity-rated **P0 / P1 / P2** ("will / might / probably won't cause a 2 a.m. call").
- **No code changes in this change.** This is a research+spec change. Fixes ship in named follow-up changes that this proposal scopes:
  - `db-credentials-canonicalize` — fix `worker/stages/store.py:27` Redis password, fix `helm/zovarc/values.yaml:134` Helm password, route every worker module through `worker/settings.py`, gate `TestPass2026` behind a required env var in healer/zvadmin/SDK/dpo.
  - `db-migration-runner-hardening` — rename the four duplicate migration files to non-conflicting numbers, teach the bash runner to insert into `schema_migrations`, wrap each migration in a transaction, fail fast on checksum mismatch.
  - `db-temporal-isolation` — document the supported customer deployment topologies (single-PG dev tier vs separate-PG production tier), add a `temporal_postgres` service to a new `docker-compose.production.yml` overlay, cap Temporal's direct Postgres connection count.
  - `db-backup-scheduling` — add a `zovark-backup` sidecar container that runs `backup-db.sh` on a cron, add restore-validation to `scripts/restore-db.sh`, document `redpanda_data` and Temporal backup procedures separately.
  - `db-resource-alerting` — wire Prometheus alerts for disk usage, WAL backlog, database growth, PgBouncer pool saturation, and Temporal visibility table size; ship a daily cleanup job that honours `data_retention_policies`.
  - `surreal-removal` — execute the removal checklist from this audit (eight call sites, one module, one compose service, six env vars).
- This change ratifies the **`db-reliability` capability** so every follow-up has a contract to test against.

## Capabilities

### New Capabilities
- `db-reliability`: defines the contract for running PostgreSQL as the sole canonical store in a customer environment — connection topology, migration safety, backup/restore guarantees, resource exhaustion guardrails, credential discipline, and the SurrealDB removal completeness criteria. The audit doc and every follow-up fix are validated against this spec.

### Modified Capabilities
<!-- None — no prior specs in openspec/specs/. -->

## Impact

- **Code (this change)**: none. Audit-only.
- **Code (follow-up changes scoped here)**: `worker/stages/store.py`, `worker/settings.py`, ~40 worker modules under `worker/{bootstrap,detection,embedding,intelligence,response,sre}/`, `helm/zovarc/values.yaml`, `agent/healer.py`, `cmd/zvadmin/queue.go`, `sdk/python/zovark/client.py`, `dpo/dpo_forge.py`, `scripts/apply_migrations.sh`, `scripts/backup-db.sh`, `scripts/restore-db.sh`, `docker-compose.yml`, `monitoring/alert_rules.yml`, `worker/surreal_graph.py`, `worker/data_plane/emit.py`, `worker/entity_graph.py`, `worker/investigation_memory.py`, `worker/intelligence/blast_radius.py`, `worker/search/semantic.py`, `worker/tools/enrichment.py`.
- **Schema**: rename four duplicate-numbered migrations (`041`, `050`, `051`, `052`); add a real `schema_migrations` row insert from the bash runner; ship a new migration that bootstraps a daily cleanup job consuming `data_retention_policies`.
- **Operational**: every service gets a tested failure mode for "PG unreachable for 30 s," "PgBouncer dies but PG healthy," and "PG dies but PgBouncer healthy." A fresh customer deploy can run `scripts/backup-db.sh` on a schedule and recover from `scripts/restore-db.sh` against an empty volume without operator intervention.
- **Docs**: `docs/DATABASE_RELIABILITY_AUDIT.md` (new), `docs/RUNBOOK_HEALTHCHECK.md` updated with the eight failure-mode procedures, `CLAUDE.md` "Known Issues" section trimmed of items resolved by the follow-up changes, `CLAUDE.md` "Environment Variables" table extended with the new required env vars (`ZOVARK_SYNTHETIC_LOGIN_PASSWORD`, etc.).
- **Tests**: integration test that simulates `docker compose stop pgbouncer` and asserts API responds 503 within 5 s + recovers within 30 s of restart. Integration test that simulates `docker compose stop postgres` and asserts the worker enters a backoff loop without crashing. Unit test that asserts every `worker/**/*.py` module that opens a database connection imports from `worker.settings`.
