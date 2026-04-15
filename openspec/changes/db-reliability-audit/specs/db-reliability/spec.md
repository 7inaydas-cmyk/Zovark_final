## ADDED Requirements

### Requirement: Audit document with severity ratings
The change SHALL ship `docs/DATABASE_RELIABILITY_AUDIT.md` covering all eight finding clusters (single-point-of-failure, Temporal isolation, migration safety, backup/recovery, resource exhaustion, PgBouncer failure modes, hardcoded credentials, SurrealDB blast radius). Every finding MUST carry a `file:line` reference and a P0 / P1 / P2 severity defined by 2 a.m.-call probability (P0 = will cause one within 30 days of GA; P1 = might under specific conditions; P2 = tech debt).

#### Scenario: Audit doc enumerates every duplicate migration
- **WHEN** a reviewer opens `docs/DATABASE_RELIABILITY_AUDIT.md` and searches for "duplicate migration"
- **THEN** the document lists all four pairs (`041_system_configs.sql` vs `041_network_beaconing_skill.sql`, `050_sprint1k_cross_tenant_entities.sql` vs `050_model_performance_tracking.sql`, `051_sprint2a_detection_rules_enhancements.sql` vs `051_bootstrap_pipeline_enhancements.sql`, `052_sprint2b_soar_playbooks_enhancements.sql` vs `052_rate_limit_audit.sql`) with file paths and a P0 rating

#### Scenario: Audit doc carries severity for every finding
- **WHEN** a reviewer scans the audit document
- **THEN** every finding has a P0, P1, or P2 label and a one-sentence "why this is a 2 a.m. call" justification

### Requirement: PostgreSQL connection topology is documented and consistent
The audit deliverable SHALL document every service that opens a database connection, whether it goes through PgBouncer (`:5432` on the `pgbouncer` host) or direct (`:5432` on the `postgres` host), the connection-string env var, the retry policy, and the healthcheck that detects a dead connection. Customer deployments MUST have a single canonical answer for "how does service X talk to PG."

#### Scenario: Every service appears in the topology table
- **WHEN** a reviewer opens the topology section of the audit doc
- **THEN** the table includes a row for `api`, `worker`, `temporal`, `postgres-exporter`, `temporal-exporter`, `worker-metrics`, and `healer`, naming the connection target (PgBouncer vs direct), the env var, the library, and the healthcheck

#### Scenario: Inconsistent routing is flagged
- **WHEN** the audit identifies that `api` connects through PgBouncer but `temporal` connects direct
- **THEN** the doc flags this as a P1 finding and proposes the production-tier topology that puts Temporal on its own Postgres instance

### Requirement: Migration runner is safe to re-run on any customer database
The migration runner SHALL be safe to invoke against any of: a fresh empty database, a database with only `init.sql` applied, a database with migrations 001–060 applied, or a database with migrations 001–072 applied. Re-running MUST be a no-op (no duplicate inserts, no failed `CREATE TABLE`). A partial-failure mid-migration MUST roll back the partially-applied migration so the next run can retry from the same point.

#### Scenario: Re-running the runner is idempotent
- **WHEN** an operator runs `scripts/apply_migrations.sh` against a database where every migration has already been applied
- **THEN** the runner exits 0, prints "no migrations to apply", and `SELECT count(*) FROM schema_migrations` is unchanged

#### Scenario: Failed migration rolls back atomically
- **WHEN** a migration with a syntax error fires
- **THEN** the runner aborts with a non-zero exit code, the partial transaction is rolled back, no row is inserted into `schema_migrations` for the failed file, and re-running the runner retries the same file

#### Scenario: Duplicate filenames are forbidden
- **WHEN** a developer adds a new file `migrations/073_foo.sql` while another `073_*.sql` already exists
- **THEN** a CI check fails with "duplicate migration number 073"

#### Scenario: Schema drift is detected at API startup
- **WHEN** the API boots against a database where `schema_migrations` is missing a file that exists on disk
- **THEN** the API logs an error naming the missing migration and refuses to start, exit code non-zero

### Requirement: Disaster recovery is testable from cold start
A scheduled backup SHALL run automatically without operator intervention. The restore procedure SHALL be documented in `docs/RUNBOOK_HEALTHCHECK.md` and SHALL succeed against a fresh empty `postgres_data` volume in under 30 minutes for databases up to 100 GB. The audit document MUST identify which named volumes are critical (`postgres_data`), which are recoverable from source (`redpanda_data`, `valkey_data`), and which are derived state (`prometheus_data`, `grafana_data`).

#### Scenario: Backup runs without operator action
- **WHEN** a customer brings up the stack with `docker compose up -d` and waits 24 hours
- **THEN** at least one timestamped backup file exists in the configured backup destination

#### Scenario: Restore from backup recreates a working database
- **WHEN** an operator follows the documented restore procedure against an empty `postgres_data` volume
- **THEN** the API reaches `GET /ready` returning 200 within 5 minutes of the restore completing

#### Scenario: Backup includes Temporal schemas
- **WHEN** the backup script runs against the dev-tier deployment where Temporal shares the application Postgres
- **THEN** the resulting dump includes `temporal` and `temporal_visibility` databases, not just `zovark`

### Requirement: Resource exhaustion is alerted and bounded
The deployment SHALL emit Prometheus alerts before disk, connection, or memory exhaustion causes a service outage. The minimum alert set MUST include: PostgreSQL disk usage, PostgreSQL database growth rate, PgBouncer pool saturation, Temporal visibility table size, and PostgreSQL WAL backlog. The `data_retention_policies` table populated by migration `013` MUST have a corresponding cleanup job that honours the configured retention windows.

#### Scenario: Disk usage alert fires before exhaustion
- **WHEN** PostgreSQL data volume reaches 85% of its filesystem capacity
- **THEN** Prometheus fires the `PostgresDiskUsageHigh` alert at warning severity, and at 95% the same alert fires at critical severity

#### Scenario: Retention policy is enforced
- **WHEN** the cleanup job runs against a database with rows in `webhook_deliveries` older than 30 days (the policy from migration `013`)
- **THEN** those rows are deleted (hard delete per policy) and `data_retention_policies.last_cleanup_at` is updated for that table

#### Scenario: PgBouncer pool saturation alerts before clients are queued
- **WHEN** PgBouncer's active connection count exceeds 90% of `MAX_DB_CONNECTIONS` for two minutes
- **THEN** Prometheus fires `PgBouncerPoolSaturation` at warning severity

### Requirement: PgBouncer and PostgreSQL failure modes are tested
Every service that opens a database connection SHALL behave deterministically when PgBouncer is unavailable but PostgreSQL is healthy, and vice versa. The audit document MUST describe the expected behaviour for each combination, and an integration test MUST simulate each failure and assert the documented behaviour.

#### Scenario: PgBouncer dies but PostgreSQL is healthy
- **WHEN** an operator runs `docker compose stop pgbouncer` while `postgres` is healthy
- **THEN** the API returns HTTP 503 from `GET /ready` within 5 seconds, and within 30 seconds of `docker compose start pgbouncer` the API returns 200 again without manual restart

#### Scenario: PostgreSQL dies but PgBouncer is healthy
- **WHEN** an operator runs `docker compose stop postgres` while `pgbouncer` is healthy
- **THEN** the worker logs a connection error, enters an exponential backoff loop, does not crash, and resumes processing within 30 seconds of `docker compose start postgres`

#### Scenario: Healthchecks distinguish the two failure modes
- **WHEN** PgBouncer is up but Postgres is down
- **THEN** PgBouncer's healthcheck reports unhealthy (it cannot reach upstream), so `depends_on` cascades work as expected

### Requirement: All database credentials flow through worker/settings.py and api/main.go
No Python or Go file in the repository SHALL hardcode a database password, Redis password, or LLM API key as a literal default outside the canonical settings modules (`worker/settings.py`, `api/main.go`, `agent/healer.py`'s env block) and the explicit allowlist (`.env.example`, `CLAUDE.md`, `migrations/seed_dev_data.sql`, dev `docker-compose.yml` defaults, `tests/`). The string `TestPass2026` MUST NOT appear in any non-test file unless it is read from an environment variable as a fallback.

#### Scenario: Worker module bypassing settings is rejected
- **WHEN** a developer adds a new file `worker/foo/bar.py` containing `os.environ.get("DATABASE_URL", "postgresql://zovark:zovark_dev_2026@postgres:5432/zovark")`
- **THEN** the credential CI test fails with the offending file:line and the wrong password literal

#### Scenario: TestPass2026 in production code is rejected
- **WHEN** a developer commits a file outside `tests/`, `scripts/seed_dev*`, or the explicit allowlist that contains the literal `TestPass2026`
- **THEN** the credential CI test fails

#### Scenario: Wrong canonical password is rejected
- **WHEN** any file (anywhere in the repo) contains the literal `zovark_dev_2026` or `zovark-redis-dev-2026`
- **THEN** the credential CI test fails — these are the wrong values that the audit identified

#### Scenario: Existing audit findings are fixed
- **WHEN** the audit's hardcoded-credential catalog is replayed against the codebase after `db-credentials-canonicalize` lands
- **THEN** zero P0 entries remain, including the `worker/stages/store.py:27` Redis password and the `helm/zovarc/values.yaml:134` Helm password

### Requirement: SurrealDB removal checklist is complete and consumable
The audit document SHALL contain a SurrealDB removal checklist that names every file, function, env var, volume, compose service, and migration touching SurrealDB. The list MUST be precise enough that the follow-up `surreal-removal` change can execute it without re-doing the research, and complete enough that a `grep -rni surreal` against the post-removal repo returns zero hits in source files (docs and historical migrations excluded).

#### Scenario: Checklist names every Python call site
- **WHEN** a reviewer reads the SurrealDB section of the audit doc
- **THEN** the list includes `worker/entity_graph.py:244,366,386`, `worker/investigation_memory.py:63,82,102`, `worker/intelligence/blast_radius.py:13,31`, `worker/search/semantic.py:91,93`, `worker/tools/enrichment.py:158,161`, and `worker/data_plane/emit.py:21,32,68-88,181-183`

#### Scenario: Checklist names every infrastructure object
- **WHEN** a reviewer reads the same section
- **THEN** the list includes the `surrealdb` compose service (`docker-compose.yml:76-110`), the `surreal_data` named volume, the six `ZOVARK_SURREAL_*` env vars, and migration `068_ticket2_surreal_graph_pgvector_retirement.sql`

#### Scenario: Post-removal grep is empty
- **WHEN** the `surreal-removal` follow-up change has landed
- **THEN** `grep -rni surreal worker/ api/ docker-compose.yml` returns zero matches

### Requirement: Follow-up changes are scoped and named
The audit deliverable SHALL identify each follow-up change by name (`db-credentials-canonicalize`, `db-migration-runner-hardening`, `db-temporal-isolation`, `db-backup-scheduling`, `db-resource-alerting`, `surreal-removal`) and MUST list which findings each change resolves. No finding from the audit may exist without an owning follow-up change.

#### Scenario: Every audit finding maps to a follow-up change
- **WHEN** a reviewer reads the audit's "Resolution" section
- **THEN** every numbered finding has a "Resolved by: <change-name>" line, and no finding maps to "TBD"

#### Scenario: Follow-up changes can be opened independently
- **WHEN** a developer runs `/opsx:propose db-credentials-canonicalize`
- **THEN** the audit doc and this spec contain enough context that the new proposal can be drafted without re-reading the source code first
