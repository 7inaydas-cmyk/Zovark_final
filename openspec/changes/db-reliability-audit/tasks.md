## 1. Audit Document — Skeleton

- [ ] 1.1 Create `docs/DATABASE_RELIABILITY_AUDIT.md` with eight top-level sections matching the eight finding clusters from the proposal: SPOF, Temporal isolation, migration safety, backup/recovery, resource exhaustion, PgBouncer failure modes, hardcoded credentials, SurrealDB blast radius.
- [ ] 1.2 At the top of the doc, add a one-page executive summary with a P0 / P1 / P2 count, a "what would page on at 2 a.m. tomorrow" summary, and a link to each section anchor.
- [ ] 1.3 Add a "Resolution" section that maps every numbered finding to its owning follow-up change (`db-credentials-canonicalize`, `db-migration-runner-hardening`, `db-temporal-isolation`, `db-backup-scheduling`, `db-resource-alerting`, `surreal-removal`).
- [ ] 1.4 Add a "Severity definitions" callout at the top: P0 = will cause a 2 a.m. call within 30 days of GA; P1 = might under specific conditions; P2 = tech debt.

## 2. Section 1 — Single Point of Failure Analysis

- [ ] 2.1 Document the seven services that touch PostgreSQL (`api`, `worker`, `temporal`, `postgres-exporter`, `temporal-exporter`, `worker-metrics`, `healer`) in a single table. Columns: service, library, env var, target (`pgbouncer` vs `postgres`), pool type, retry policy, healthcheck.
- [ ] 2.2 Cite `api/main.go:89,158-162`, `api/db.go:49-82,71-82,84-126`, `api/handlers.go:36-119,125-194` for the API path.
- [ ] 2.3 Cite `worker/database/pool_manager.py:1-110,46-56,85-90`, `worker/main.py:127,154-163`, `worker/stages/ingest.py:63-91`, `worker/stages/govern.py:22-49` for the worker path. Note the asymmetry: API depends on PgBouncer healthy (`docker-compose.yml:389`) but worker does not.
- [ ] 2.4 Cite `agent/healer.py:56-58,343-357` for the healer's `pg_isready` exec-based check; note it's read-only and never opens an actual SQL connection.
- [ ] 2.5 Cite `docker-compose.yml:608-626` (postgres-exporter), `:530-549` (temporal-exporter), `:649-673` (worker-metrics) and call out that all three connect direct to `postgres:5432`, NOT through PgBouncer.
- [ ] 2.6 Document what happens for each service when PG is unreachable for 30 s: which crash, which loop forever, which return 503, which silently fail. Mark each row P0/P1/P2.
- [ ] 2.7 Resolution: `db-credentials-canonicalize` (worker fallback path) + `db-temporal-isolation` (Temporal direct connection).

## 3. Section 2 — Temporal Isolation

- [ ] 3.1 Quote `docker-compose.yml:224-249` showing Temporal env: `DB=postgres12`, `POSTGRES_SEEDS=postgres`, `DB_PORT=5432` — Temporal connects direct to `postgres:5432`, NOT through PgBouncer.
- [ ] 3.2 Note that Temporal auto-creates `temporal` and `temporal_visibility` databases (image `temporalio/auto-setup:1.24.2`) and shares the same physical Postgres instance as the application.
- [ ] 3.3 Cite `config/postgresql.conf:6` (`max_connections = 200`) and the comment-budget on line 4-5 (PgBouncer=50, API=25, Temporal=15, Embedding=5, Admin=5, Reserve=20). Note Temporal's actual usage is undocumented in the auto-setup image and could exceed 15.
- [ ] 3.4 Document the contention scenario: a slow application query (e.g., recursive CTE on `entity_edges`) holding 25 PgBouncer-pooled connections + Temporal holding 15 direct connections + monitoring exporters holding 3 = 43 of 200. Spike to 200 if the worker's three pool tiers (`pool_manager.py`) all saturate. Rate this **P1** — possible under load but not certain.
- [ ] 3.5 Recommend the production-tier overlay (`db-temporal-isolation`): a separate `temporal-postgres` container so app queries can never starve workflow polling.
- [ ] 3.6 Resolution: `db-temporal-isolation`.

## 4. Section 3 — Migration Safety

- [ ] 4.1 List all four duplicate-numbered migrations with file paths, sizes, and first-line headers: `041_system_configs.sql` vs `041_network_beaconing_skill.sql`, `050_sprint1k_cross_tenant_entities.sql` vs `050_model_performance_tracking.sql`, `051_sprint2a_detection_rules_enhancements.sql` vs `051_bootstrap_pipeline_enhancements.sql`, `052_sprint2b_soar_playbooks_enhancements.sql` vs `052_rate_limit_audit.sql`. Rate **P0**.
- [ ] 4.2 Cite `scripts/apply_migrations.sh:135` for the bash runner and `api/migrate.go:35-44,49,132,138` for the Go runner. Note that the bash runner does NOT insert into `schema_migrations` — that's only the Go runner. Rate **P0**.
- [ ] 4.3 Cite `scripts/apply_migrations.sh:115-131` for the special `068` handling and explain the SurrealDB cutover gating.
- [ ] 4.4 Document `init.sql` mounting (`docker-compose.yml:22`) at `/docker-entrypoint-initdb.d/01-init.sql`; explain that `init.sql` runs only on first boot of an empty volume, and migrations replay on top.
- [ ] 4.5 Document `migrations/seed_dev_data.sql` mounting (`docker-compose.yml:26`) at `02-seed-dev.sql`; cite the `BEGIN;` wrap and `ON CONFLICT (id) DO NOTHING` (lines 19, 29, 42, 45, 64) idempotency pattern.
- [ ] 4.6 Document the failure scenario: customer's PG has `001-060` applied, ships `061-072`. With duplicate numbering, the bash runner's lex-sort applies `041_network_beaconing_skill.sql` after `041_system_configs.sql` — but both are tagged `041`. If the `schema_migrations` ledger ever syncs from filename only, they collide. Rate **P0**.
- [ ] 4.7 Resolution: `db-migration-runner-hardening`.

## 5. Section 4 — Backup and Recovery

- [ ] 5.1 List all named volumes in `docker-compose.yml` (lines ~1043-1061). For each, mark Critical / High / Medium / Low and the recovery source. Critical = `postgres_data`. High = `redpanda_data`, `surreal_data`. Medium = `redis_data` (or `valkey_data`), `minio_data`. Low = everything else.
- [ ] 5.2 Cite `scripts/backup-db.sh:57-65` (pg_dump invocation), `:80-93` (optional GPG), `:99` (MinIO upload), `:26-27,107-130` (retention policy: 7 daily + 4 weekly), `:68-74` (PIPESTATUS error handling).
- [ ] 5.3 Cite `scripts/restore-db.sh:31-40,64,68-73,79-81,84-87,90,93-96` for the restore flow. Flag the missing pre-restore integrity check as **P1**.
- [ ] 5.4 Confirm by inspection that no cron, no systemd timer, no compose service runs `backup-db.sh` automatically. Rate **P0**.
- [ ] 5.5 Confirm that Temporal's databases (`temporal`, `temporal_visibility`) are NOT in the current backup script. Rate **P0**.
- [ ] 5.6 Confirm that `redpanda_data` and `surreal_data` are not backed up at all. Rate **P1** for redpanda (replayable from source), **P2** for surreal (canonical store is Postgres post-fix).
- [ ] 5.7 Document the minimal disaster-recovery procedure for an IT admin: backup destination → restore command → first-boot validation. Plain English, no kubectl.
- [ ] 5.8 Resolution: `db-backup-scheduling`.

## 6. Section 5 — Disk and Resource Exhaustion

- [ ] 6.1 Quote the memory limits from `docker-compose.yml`: postgres 2G, valkey 128M, pgbouncer 128M, temporal 512M, api 256M, worker 512M, healer 512M, dashboard 128M, redpanda 1G, surrealdb 512M. Sum ≈ 3.1 GB on a 4 GB host.
- [ ] 6.2 Quote `config/postgresql.conf` lines 6 (`max_connections=200`), 7-13 (`shared_buffers`, `effective_cache_size`, `work_mem`, `maintenance_work_mem`, `wal_buffers`, `max_wal_size`, `min_wal_size`, `checkpoint_completion_target`), 27-30 (replication settings), 36-38 (TCP keepalives), 48-52 (autovacuum). Note the budget comment vs PgBouncer's `MAX_DB_CONNECTIONS=50`.
- [ ] 6.3 Quote `migrations/013_sprint3f_security.sql` showing the `data_retention_policies` table and its seed rows (`agent_audit_log`=365d archive, `webhook_deliveries`=30d hard, `usage_records`=90d hard, `investigation_steps`=180d archive, `siem_alerts`=90d soft). Confirm by grep that NO worker code reads from this table. Rate **P0**.
- [ ] 6.4 Quote `monitoring/alert_rules.yml` listing every existing alert (HighPendingTasks, HighTaskFailureRate, PostgresConnectionsHigh, RedisMemoryHigh, NoActiveWorkers, HighLLMLatency). Note the absence of disk, WAL, database growth, and PgBouncer pool saturation alerts. Rate **P0**.
- [ ] 6.5 Document the four exhaustion scenarios from the audit research: WAL fills disk, postgres_data fills volume, PgBouncer hits MAX_DB_CONNECTIONS, Temporal visibility table grows unbounded. For each, name the symptom, the time-to-failure under typical load, and the lack of any auto-cleanup or alert.
- [ ] 6.6 Cite `scripts/cleanup_temporal.sh:1-59` and note it terminates legacy V1 workflows but does NOT clean visibility tables. Cite `temporal-config/development-sql.yaml` showing only `limit.maxIDLength: 255` — no retention config.
- [ ] 6.7 Resolution: `db-resource-alerting`.

## 7. Section 6 — PgBouncer Failure Modes

- [ ] 7.1 Quote `docker-compose.yml:188-221` for the full PgBouncer service definition: image `edoburu/pgbouncer:latest`, container `zovark-pgbouncer`, pool mode `transaction`, `MAX_CLIENT_CONN=400`, `DEFAULT_POOL_SIZE=25`, `MIN_POOL_SIZE=5`, `RESERVE_POOL_SIZE=20`, `RESERVE_POOL_TIMEOUT=3`, `MAX_DB_CONNECTIONS=50`, `SERVER_IDLE_TIMEOUT=300`, `QUERY_TIMEOUT=30`, `AUTH_TYPE=scram-sha-256`, `restart=unless-stopped`.
- [ ] 7.2 Document the API's hard dependency: `docker-compose.yml:389` (`depends_on: pgbouncer: condition: service_healthy`). API will not start without PgBouncer healthy. Rate **P1**.
- [ ] 7.3 Document the worker's MISSING dependency: `docker-compose.yml:453-463` shows worker depends on `postgres`, NOT pgbouncer. Worker will start before PgBouncer and silently fall back to direct PG via `worker/database/pool_manager.py:85-90`. Rate **P0** — the fallback uses a different connection string and may have a different password.
- [ ] 7.4 Quote `docker-compose.yml:206-212` for the PgBouncer healthcheck (`pg_isready -h 127.0.0.1 -p 5432 -U zovark`). Note it only probes its own loopback, NOT the upstream Postgres. PgBouncer can be "healthy" while completely unable to reach PG. Rate **P1**.
- [ ] 7.5 Quote `CLAUDE.md:602` and `api/db.go:178-181` for the SET LOCAL transaction-pooling caveat. Note this is a known compatibility constraint, not a bug, but documented for completeness.
- [ ] 7.6 Quote `api/db.go:84-126` (selfTestPool) showing the prepared-statement collision detection at startup. Document that this is correct behaviour and prevents a class of 08P01 errors at runtime.
- [ ] 7.7 Document the four failure modes: (a) PgBouncer dies, PG healthy → API 503, worker falls through to direct PG (with credential drift risk). (b) PG dies, PgBouncer healthy → both API and worker fail every query, neither restarts automatically until PG is back. (c) PgBouncer slow → connection queue grows, eventually `RESERVE_POOL_TIMEOUT=3s` kicks in and clients see errors. (d) Stale connections survive PG restart for up to ~10 minutes due to TCP keepalive defaults (`postgresql.conf:36-38`).
- [ ] 7.8 Resolution: `db-credentials-canonicalize` (worker fallback consistency) + `db-resource-alerting` (PgBouncer healthcheck enhancement).

## 8. Section 7 — Hardcoded Credential Catalog

- [ ] 8.1 Tabulate every P0 hit. **Critical entry:** `worker/stages/store.py:27` uses `redis://:zovark-redis-dev-2026@redis:6379/0` — the WRONG Redis password. Dedup writebacks have been silently failing in dev for months. Rate **P0**.
- [ ] 8.2 Add **P0:** `helm/zovarc/values.yaml:134` ships `databaseUrl: "postgresql://zovark:zovark_dev_2026@postgresql:5432/zovark"` — the WRONG DB password. Any Helm-deployed customer fails to connect on first boot. Rate **P0**.
- [ ] 8.3 Add **P0:** docker-compose.yml uses `${POSTGRES_PASSWORD:-zovark_dev_2026}` as the default in 5 places (lines 368, 412, 536, 612, 655). The `:-` default is the WRONG password. Without `POSTGRES_PASSWORD` set in the env, the entire stack fails. Rate **P0**.
- [ ] 8.4 List the **40+ worker modules** that hardcode `zovark_dev_2026` (the wrong password) as `os.environ.get("DATABASE_URL", …)` defaults: `worker/bootstrap/activities.py:19`, `worker/bootstrap/cisa_kev.py:132,195`, `worker/bootstrap/mitre_attack.py:234`, `worker/correlation/engine.py:15`, `worker/detection/pattern_miner.py:15`, `worker/detection/rule_generator.py:31`, `worker/detection/rule_validator.py:24`, `worker/detection/sigma_generator.py:26`, `worker/detection/workflow.py:18`, `worker/embedding/batch.py:15`, `worker/embedding/versioning.py:15`, `worker/entity_graph.py:23`, `worker/finetuning/evaluation.py:17`, `worker/intelligence/cross_tenant.py:16`, `worker/intelligence/cross_tenant_workflow.py:15`, `worker/intelligence/fp_analyzer.py:18`, `worker/investigation_cache.py:58,147,206,289`, `worker/investigation_memory.py:58`, `worker/llm_logger.py:58`, `worker/models/registry.py:15`, `worker/pii_detector.py:22`, `worker/reporting/incident_report.py:17`, `worker/response/actions.py:31`, `worker/response/auto_trigger.py:14`, `worker/response/playbook_engine.py:28`, `worker/response/workflow.py:15`, `worker/scheduler/workflow.py:17`, `worker/search/semantic.py:15`, `worker/shadow.py:27`, `worker/sla/monitor.py:15`, `worker/sre/applier.py:13`, `worker/sre/monitor.py:13`, `worker/sre/patcher.py:14`, `worker/stampede.py:35`, `worker/token_quota.py:19`, `worker/tools/runner.py:347`, `worker/training/trigger.py:16`. Rate **P0**.
- [ ] 8.5 List the **TestPass2026 in production-path files**: `agent/healer.py:81`, `cmd/zvadmin/queue.go:125`, `sdk/python/zovark/client.py:9`, `dpo/dpo_forge.py:471`. Rate **P1**.
- [ ] 8.6 List the canonical-but-allowed locations as a separate "allowlist" sub-section: `worker/settings.py:40,46,150`, `.env.example:51,58`, `CLAUDE.md:38`, `migrations/049_sprint1e_hardening.sql:17`, `worker/redis_client.py:10`, `worker/_legacy_activities.py:130,147,165`, `tests/conftest.py:13`, `tests/benchmark/run_benchmark.py:17`, `worker/tests/test_synthetic_login.py:30`, `migrations/seed_dev_data.sql:19,45,64`. These are NOT bugs — they exist intentionally for dev / test / docs. Rate **P2** as documentation-only.
- [ ] 8.7 List LLM key (`sk-zovark-dev-2026`) appearances and confirm none are in customer-facing default paths: `worker/stages/analyze.py:52` (only in `except ImportError` fallback), `worker/stages/assess.py:54`, `worker/finetuning/evaluator.py:18`, `docker-compose.optional.yml:15`, `helm/zovarc/values.yaml:107,136`. Rate **P2** — documented as dev-only and the API enforces non-empty in production.
- [ ] 8.8 Confirm JWT_SECRET enforcement is correct: `api/main.go:92,145` (defaults to empty string, fails fast if < 32 chars). Mark as **PASS — no action needed**.
- [ ] 8.9 Confirm no `postgres:postgres` anonymous defaults exist anywhere in the repo. Mark as **PASS**.
- [ ] 8.10 Resolution: `db-credentials-canonicalize`.

## 9. Section 8 — SurrealDB Removal Checklist

- [ ] 9.1 Document the core module: `worker/surreal_graph.py:1-560` containing 11 functions (`_surreal_enabled`, `surreal_sql`, `surreal_sql_sync`, `write_entity_graph_surreal`, `semantic_search_surreal`, `upsert_investigation_vector_surreal`, `investigation_memory_exact_surreal`, `investigation_memory_semantic_surreal`, `blast_radius_surreal`, `surreal_entity_reachability`, `surreal_entity_reachability_sync`).
- [ ] 9.2 Document the compose service: `docker-compose.yml:76-110` — image `surrealdb/surrealdb:v2.1.4`, container `zovark-surrealdb`, port `127.0.0.1:8008:8000`, volume `surreal_data`, command `start --user root --pass ${SURREAL_ROOT_PASSWORD:-change-me-surreal} rocksdb:/data/zovark.db?sync=every`.
- [ ] 9.3 Document the named volume `surreal_data` (top-level `volumes:` block).
- [ ] 9.4 Document the env vars: `ZOVARK_SURREAL_HTTP_URL`, `ZOVARK_SURREAL_PASSWORD`, `ZOVARK_SURREAL_ENABLED` (set in `docker-compose.yml:440-442`); `ZOVARK_SURREAL_USER`, `ZOVARK_SURREAL_NS`, `ZOVARK_SURREAL_DB` (set in `.env.example:156-168`); plus the corresponding settings fields in `worker/settings.py:114-119`.
- [ ] 9.5 Document `surrealdb` listed in the `NO_PROXY` env var at `docker-compose.yml:447`.
- [ ] 9.6 Document each call site as a phase-2 removal target:
  - `worker/entity_graph.py:244,246-254` — `write_entity_graph_surreal()`
  - `worker/entity_graph.py:364,366` — `upsert_investigation_vector_surreal()`
  - `worker/entity_graph.py:383,386` — `semantic_search_surreal()`
  - `worker/investigation_memory.py:63,82` — `investigation_memory_exact_surreal()`
  - `worker/investigation_memory.py:63,102` — `investigation_memory_semantic_surreal()`
  - `worker/intelligence/blast_radius.py:13,31` — `blast_radius_surreal()`
  - `worker/search/semantic.py:91,93` — `semantic_search_surreal()`
  - `worker/tools/enrichment.py:158,161` — `surreal_entity_reachability_sync()`
  - `worker/data_plane/emit.py:21,32,68-88,181-183` — `_emit_surreal()`
- [ ] 9.7 Document the migration: `migrations/068_ticket2_surreal_graph_pgvector_retirement.sql` (the migration that retired pgvector and deferred the entity graph to SurrealDB). Note that this migration's spirit is reversed by the entity-graph fix change.
- [ ] 9.8 Document the test file references: `worker/tests/test_data_plane_emit.py:15-23,78`.
- [ ] 9.9 Document the duplicate feature-flag check in `worker/tools/enrichment.py:156` that should be replaced with `_surreal_enabled()`.
- [ ] 9.10 Document the dashboard tag reference: `config/signoz/dashboards/database_observability.json:5`.
- [ ] 9.11 Document the script reference: `scripts/apply_migrations.sh:8` (comment mentioning 068).
- [ ] 9.12 Add a six-phase removal checklist at the end of the section: Phase 1 disable, Phase 2 remove call sites, Phase 3 delete modules, Phase 4 clean infrastructure, Phase 5 documentation, Phase 6 settings cleanup. This is the consumable checklist for the `surreal-removal` follow-up change.
- [ ] 9.13 Resolution: `surreal-removal`.

## 10. Cross-Cutting

- [ ] 10.1 Add a "Severity Roll-Up" table at the top of the audit doc: total P0 / P1 / P2 counts across all eight sections. Should sum to roughly P0=11, P1=8, P2=10 based on the research; verify after writing.
- [ ] 10.2 Add a "What ships first" recommendation that mirrors design.md's migration plan: week 1 = `db-credentials-canonicalize` + `db-migration-runner-hardening`; week 2 = `db-backup-scheduling` + `db-resource-alerting`; week 3 = `db-temporal-isolation`; post-week-3 = `surreal-removal`.
- [ ] 10.3 Cross-link the audit doc from `docs/RUNBOOK_HEALTHCHECK.md` and `docs/ARCHITECTURE.md`.
- [ ] 10.4 Update `CLAUDE.md` "Known Issues" section to add a single bullet: "Database reliability audit complete — see `docs/DATABASE_RELIABILITY_AUDIT.md`. Six follow-up changes scoped under `openspec/changes/db-*`."
- [ ] 10.5 Verify against the spec scenarios: every Requirement in `specs/db-reliability/spec.md` is grounded in a section of the audit doc. Add cross-references where missing.

## 11. Sign-Off

- [ ] 11.1 Run `openspec validate db-reliability-audit --strict` and confirm zero errors.
- [ ] 11.2 Confirm zero code files were modified (`git diff --stat -- ':!docs/' ':!openspec/' ':!CLAUDE.md'` returns empty).
- [ ] 11.3 Open the audit doc in a markdown preview and check that every `file:line` reference resolves to a real line in the current commit (catch any drift from the parallel research).
- [ ] 11.4 Hand the audit doc to a second reviewer for sanity check before opening any of the six follow-up `/opsx:propose` calls.
