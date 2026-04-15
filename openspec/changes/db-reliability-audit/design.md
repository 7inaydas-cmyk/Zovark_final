## Context

Zovark v3.2.1 is a single-node air-gap deployment (`docker compose up -d`) that ships PostgreSQL 16 + PgBouncer + Temporal + Valkey + Redpanda as core services. Customers run it in regulated environments (CMMC, HIPAA, GDPR) where a 2 a.m. page is a contractual incident, not an inconvenience. Today the database tier is one PostgreSQL container with no replica, no scheduled backup, no disk alerting, and a fan-out of 50+ writers and readers spread across Go, Python, and embedded SQL — many of which hardcode the wrong password.

The audit pass produced eight finding clusters, captured here verbatim with `file:line` evidence. This design document does not invent new architecture; it ratifies what the customer-tier deployment must look like and decomposes the work into named follow-up changes.

### Finding cluster summary

| # | Cluster | Severity | Evidence |
|---|---------|----------|----------|
| 1 | API hard-fails without PgBouncer; worker silently falls back to direct PG | P0 | `api/main.go:158-162`, `docker-compose.yml:389`, `worker/database/pool_manager.py:85-90` |
| 2 | Temporal direct to `postgres:5432`, bypasses PgBouncer's `MAX_DB_CONNECTIONS=50` | P1 | `docker-compose.yml:232`, `:200`, `config/postgresql.conf:6` |
| 3 | Four duplicate migration numbers (`041`, `050`, `051`, `052`); bash runner never inserts into `schema_migrations` | P0 | `migrations/041_*.sql` (×2), `scripts/apply_migrations.sh:135`, `api/migrate.go:138` |
| 4 | `backup-db.sh` exists and works but no scheduler; no Temporal/Redpanda backup | P0 | `scripts/backup-db.sh`, `docker-compose.yml` (no cron service) |
| 5 | `data_retention_policies` table populated but no consumer; no disk/WAL alerts | P0 | `migrations/013_sprint3f_security.sql`, `monitoring/alert_rules.yml` |
| 6 | API depends_on PgBouncer; worker doesn't. PgBouncer healthcheck only probes loopback | P1 | `docker-compose.yml:389`, `:206-212`, `:453-463` |
| 7 | 40+ worker modules hardcode `zovark_dev_2026` (wrong password); `store.py:27` hardcodes wrong Redis pwd; `TestPass2026` in 4 production-path files | P0 | enumerated in audit doc |
| 8 | SurrealDB has 11 functions, 8 callers, 1 service, 6 env vars, 1 volume, 1 migration | P1 | `worker/surreal_graph.py`, `docker-compose.yml:76-110` |

## Goals / Non-Goals

**Goals:**
- Produce a single audit document that names every database-tier risk, classified by 2 a.m.-call probability, with `file:line` evidence for every claim.
- Define the `db-reliability` capability so every follow-up change has a contract to test against (no "we fixed it" without a scenario).
- Scope the follow-up changes precisely enough that each one fits in a single `/opsx:propose` and ships independently — no monster PRs.
- Make the customer-tier deployment topology explicit: which services share PG, which get their own, where PgBouncer sits, what the connection budget is.
- Catalogue the SurrealDB removal points so the entity-graph fix has a single checklist to consume.

**Non-Goals:**
- Implementing any of the fixes in this change. This change is research + spec only. Code changes are explicitly out of scope and any PR with a non-doc diff fails review.
- Designing a multi-node HA topology. Single-node Postgres with backups is the goal; streaming replication is documented as "future work" but not specified.
- Re-introducing pgvector or any embedding feature. Migration `068` retired pgvector deliberately and the audit doc only catalogues the drop sites; resurrection is a separate proposal.
- Changing the dev-tier passwords. `hydra_dev_2026` and `hydra-redis-dev-2026` stay as canonical defaults — the audit catalogues *wrong* values and divergent values, not the canonical ones.
- Picking a specific scheduler (cron-in-container vs systemd-on-host vs k8s CronJob). The follow-up `db-backup-scheduling` change picks; this proposal only requires that *some* scheduler exists.

## Decisions

### Decision 1: Audit doc is the single source of evidence; the spec is the contract

**Choice:** `docs/DATABASE_RELIABILITY_AUDIT.md` records every finding with `file:line` and a P0/P1/P2 rating. `openspec/changes/db-reliability-audit/specs/db-reliability/spec.md` records the WHEN/THEN scenarios that the eventual fixes must satisfy. The audit doc is descriptive; the spec is normative.

**Why:** Splitting evidence from contract lets the audit doc rot gracefully (file lines drift) while the spec stays testable forever. A test for "PgBouncer dies, API responds 503 within 5 s" doesn't care which `api/main.go` line wires the readiness check.

**Alternatives considered:**
- *One file with both* — Rejected. The audit's value is its file:line refs, which date instantly. The spec's value is its scenarios, which are durable. They have different lifetimes.
- *Spec only, no audit doc* — Rejected. Reviewers need to see the evidence behind the severity ratings, otherwise "P0" is a vibe.

### Decision 2: P0 / P1 / P2 severities are defined by call-probability, not impact magnitude

**Choice:**
- **P0 — will cause a 2 a.m. call** in a customer environment within 30 days of GA. Wrong defaults that silently fail, missing backup scheduling, missing disk alerts, hardcoded test passwords in production paths.
- **P1 — might cause a 2 a.m. call** under specific conditions (load spikes, partial failures, schema upgrades). Temporal contention, PgBouncer healthcheck gaps, SurrealDB blast radius (fragile but currently disabled by default).
- **P2 — tech debt** that won't page anyone but degrades maintainability or audit posture. Documentation drift, dev-only credential conventions, optional service hygiene.

**Why:** The user explicitly framed this as "never get a 2 a.m. call." A severity scheme that ranks by call-probability filters fix priority correctly: P0 ships in week one, P1 in the GA window, P2 whenever.

**Alternatives considered:**
- *CVSS-style scoring* — Rejected. Overkill for a database reliability audit; produces false precision.
- *RTO/RPO impact* — Rejected. Useful for the backup section only; doesn't apply to credential or migration findings.

### Decision 3: PostgreSQL stays the canonical store; Temporal stays in the same instance for dev tier and gets its own instance for production tier

**Choice:**
- **Dev tier** (`docker-compose.yml`, what we ship today): Temporal + application share one Postgres. Document the contention risk in the audit doc; mitigate by capping Temporal's direct connection count and by raising `max_connections` to 200 (already done in `config/postgresql.conf:6`).
- **Production tier** (new `docker-compose.production.yml` overlay, follow-up change): Temporal gets a dedicated `temporal-postgres` container on the same image but a different volume. The application Postgres is sized for application data only.

**Why:** Customers are not all on Kubernetes, but the ones with strict SLAs need Temporal isolation. A compose overlay is the cheapest way to provide both topologies without forking the codebase.

**Alternatives considered:**
- *Force everyone onto separate Postgres* — Rejected. Doubles RAM and disk for dev installs and the small-customer tier that doesn't need it.
- *Move Temporal off Postgres entirely* (Cassandra, MySQL) — Rejected. Adds a third datastore family to operate; we already have Postgres expertise.

### Decision 4: Migration runner gets a real ledger; duplicate filenames get renamed once

**Choice:**
- Rename the four duplicate-numbered migrations to fresh trailing numbers (`041_network_beaconing_skill.sql` → `073_network_beaconing_skill.sql`, etc.). This is a one-time disruption that fixes ordering forever.
- Teach `scripts/apply_migrations.sh` to insert into `schema_migrations` with `(filename, checksum, applied_at, applied_by)` after every successful migration, matching what `api/migrate.go` already does.
- Wrap each migration in `BEGIN; … COMMIT;` so partial failures roll back. `ON_ERROR_STOP=1` already aborts the run.
- On startup, the API reads `schema_migrations` and refuses to start if a known migration is missing OR a checksum mismatch indicates a migration was edited after application.

**Why:** Duplicate numbers are a footgun forever; rename once, never again. A real ledger lets us answer "was 068 applied to this customer's DB?" without grepping logs.

**Alternatives considered:**
- *Adopt golang-migrate or goose* — Rejected. Adds a dependency and migrates 70+ files into a new format. The existing bash + Go runners already work; they just need a ledger.
- *Leave duplicates and document them* — Rejected. The audit shows the bash runner sorts lexicographically and applies them in non-deterministic relative order. This is a P0; the cost of a one-time rename is lower than the cost of a customer hitting it.

### Decision 5: Backups ship with a scheduler container, not just a script

**Choice:**
- Add a new `zovark-backup` service to `docker-compose.yml` running an Alpine cron image that calls `scripts/backup-db.sh` daily at 02:00 UTC by default. The schedule is overridable via `ZOVARK_BACKUP_CRON` env var.
- Backup-db.sh is extended to also `pg_dump` the Temporal databases (`temporal`, `temporal_visibility`) since they live in the same instance.
- Restore-db.sh is extended to validate against a `_test` database before the real DROP, refusing to proceed if the dump is corrupt.
- Add a separate, documented procedure for `redpanda_data` (Kafka topic snapshot via `rpk topic` export) — but do not automate it in this change. Kafka topics are replayable from source; the audit doc says so explicitly.

**Why:** A backup script that nobody runs is worse than no backup, because it gives operators false confidence. Bundling the scheduler eliminates the "did you set up cron?" question entirely.

**Alternatives considered:**
- *Document cron in the runbook and let operators wire it* — Rejected. The audit is explicit that this is a 2 a.m.-call risk; we own the scheduler.
- *Use pg_basebackup / streaming backup* — Rejected for v3.2.1. Adds replica setup overhead, doesn't fit single-node deploys, and pg_dump is sufficient at expected data sizes (under 100 GB).

### Decision 6: Resource alerting is required, not optional

**Choice:**
- Add Prometheus alerts to `monitoring/alert_rules.yml`:
  - `PostgresDiskUsageHigh` — `node_filesystem_avail_bytes{mountpoint="/var/lib/postgresql/data"} / node_filesystem_size_bytes < 0.15` for 5 min → warning; `< 0.05` → critical.
  - `PostgresDatabaseGrowthHigh` — `pg_database_size_bytes{datname="zovark"}` increased by > 5 GB in last 24 h → warning.
  - `PostgresWALBacklog` — `pg_replication_slots_pg_wal_lsn_diff` > 1 GB → warning.
  - `PgBouncerPoolSaturation` — `pgbouncer_pools_cl_active / pgbouncer_pools_max_conn > 0.9` for 2 min → warning.
  - `TemporalVisibilityTableSize` — `pg_table_size{table="executions_visibility"}` > 5 GB → warning.
- Ship a daily cleanup job (sidecar container on cron) that consumes `data_retention_policies` and runs the corresponding `DELETE` / archive operations.
- Make the `monitoring` profile required for production deployments (document in deployment guide); dev installs can still skip it.

**Why:** The audit says retention policies exist as a table but no consumer has ever been written. Either delete the table or wire the consumer; we wire the consumer because compliance customers ask for retention enforcement.

**Alternatives considered:**
- *pg_cron extension* — Rejected. Requires installing an extension, complicates the air-gap image story.
- *Per-table partition rotation* — Deferred. The partitioned tables (`investigations_2026_*`, `audit_events_2026_*`) need rotation, but that's a separate change scoped under `db-resource-alerting`.

### Decision 7: Credential discipline is enforced by a unit test, not by code review

**Choice:**
- The follow-up `db-credentials-canonicalize` change adds a unit test that:
  1. Greps every `worker/**/*.py` for `os.environ.get("DATABASE_URL"` and asserts each match is followed by `or settings.database_url` or imported via `from worker.settings import settings`.
  2. Greps the entire repo (excluding `tests/`, `scripts/seed_dev*`, `migrations/seed_dev_data.sql`, `.example` files) for `TestPass2026` and fails if found.
  3. Greps the entire repo for `zovark_dev_2026` (the wrong password) and `zovark-redis-dev-2026` (the wrong Redis password) and fails if found anywhere.
  4. Greps for `hydra_dev_2026` and `hydra-redis-dev-2026` (the canonical values) outside the allowlist (`worker/settings.py`, `.env.example`, `CLAUDE.md`, `migrations/seed_dev_data.sql`, dev `docker-compose.yml` defaults, test fixtures).
- The test runs in CI on every PR. A new file with a hardcoded credential breaks the build.

**Why:** Code review missed this for 40+ files. A grep-based test is unmissable. The audit shows the convention drift was gradual; the test stops the drift.

**Alternatives considered:**
- *Pre-commit hook only* — Rejected. Pre-commit hooks can be bypassed and aren't enforced for PRs from forks.
- *secrets-scanning service (gitleaks, trufflehog)* — Considered as a complement, not a replacement. The repo-specific test catches the *wrong* canonical values, which a generic scanner doesn't know about.

### Decision 8: SurrealDB stays disabled by default; the removal checklist is the audit's deliverable

**Choice:** This change does NOT remove SurrealDB. It catalogues the 11 functions, 8 callers, 1 compose service, 6 env vars, 1 volume, and 1 migration so the entity-graph fix change can consume the list. The `surreal-removal` follow-up change executes Phases 1–6 of the checklist.

**Why:** The user said no code changes in this audit. SurrealDB is currently gated behind `ZOVARK_SURREAL_ENABLED=false` by default; the blast radius is real but not on fire.

**Alternatives considered:**
- *Remove SurrealDB in this change* — Rejected. Out of scope per user instructions.
- *Promise the entity-graph fix change will handle it without a checklist* — Rejected. The entity-graph change already has its own scope (PostgreSQL canonical, schema reconciliation, etc.); piling SurrealDB removal into it makes both changes harder to review.

## Risks / Trade-offs

- **Risk: The audit doc rots within a release cycle as line numbers shift.** → Mitigation: every claim is also encoded as a WHEN/THEN scenario in `specs/db-reliability/spec.md`. The spec is the contract; the doc is the snapshot. Drift in the doc is acceptable as long as the spec scenarios still pass.

- **Risk: Renaming duplicate migrations breaks customers who already applied them under the old names.** → Mitigation: the rename happens in `db-migration-runner-hardening`, and that change's first task is to add an alias map so the new ledger recognizes both old and new filenames. Customers who applied `041_network_beaconing_skill.sql` under that name see it correctly recorded as `073_network_beaconing_skill.sql` after the upgrade with no replay.

- **Risk: Forcing `db-credentials-canonicalize` to fix 40+ files in one PR creates a giant diff.** → Mitigation: the follow-up change can split by directory (`worker/bootstrap/`, `worker/detection/`, etc.) into ~5 sub-PRs, but the unit test only goes green when all are merged. Acceptable: the test fails until the cleanup is complete, which forces completion.

- **Risk: The new `zovark-backup` sidecar adds another container that can fail.** → Mitigation: the sidecar exits non-zero on failure and the healer agent catches the dead container; backup failures page the operator like any other service. Better than silent missed backups.

- **Trade-off: Wrapping every migration in `BEGIN; … COMMIT;` blocks DDL operations that need their own transaction (e.g., `CREATE INDEX CONCURRENTLY`).** → The runner gets an opt-out marker (`-- migrate:no-transaction` as the first line of the file). Documented in the runner change.

- **Trade-off: Daily cleanup job that honours `data_retention_policies` will delete data customers expected to keep.** → The first iteration runs in `dry_run` mode for 7 days (logs only), then switches to enforcement after operator confirmation. Documented in `db-resource-alerting`.

- **Trade-off: Capping Temporal's connection count requires editing the compose env block, which we don't normally touch for upstream images.** → Acceptable. `temporalio/auto-setup` honours `NUM_HISTORY_SHARDS` and connection-related env vars; we set them to known-good values and document the override.

## Migration Plan

This change ships zero code, so "migration" here means the rollout order of the follow-up changes:

1. **Land this audit doc + spec first** so reviewers have a single source of truth.
2. **`db-credentials-canonicalize`** — highest P0 density, smallest surface area per file. Ship in week 1.
3. **`db-migration-runner-hardening`** — must land before any new schema migration. Ship in week 1.
4. **`db-backup-scheduling`** — backup is the most-asked customer feature. Ship in week 2.
5. **`db-resource-alerting`** — requires monitoring profile to be enabled. Ship in week 2.
6. **`db-temporal-isolation`** — only blocks the highest-tier customers. Ship in week 3.
7. **`surreal-removal`** — gated on the entity-graph fix landing first. Ship after week 3.

Rollback for the audit doc itself: `git revert`. The spec and doc are pure additions; nothing else depends on them until a follow-up change tries to satisfy a scenario.

## Open Questions

- Do we want a single combined "DB reliability week" PR train, or should each follow-up be its own merge with its own release tag? Default: separate merges, separate releases, so customers can adopt fixes incrementally.
- Should `data_retention_policies` enforcement default to on or off in v3.2.1 → v3.3? Default: off in v3.2.1, dry-run for one release, on by default in v3.3.
- Is the `temporal_postgres` overlay compatible with the existing `auto-setup` image's expectations? Confirm by spinning up the overlay locally before specifying it. **Default assumption: yes**, since `POSTGRES_SEEDS` already takes a hostname.
- For the credential test, do we allow `TestPass2026` in `e2e/docker-compose.test.yml`? Default: yes, that file is in the allowlist; production compose files are not.
