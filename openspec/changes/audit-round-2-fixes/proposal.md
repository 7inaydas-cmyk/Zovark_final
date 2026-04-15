## Why

A second deep audit of the codebase — targeting everything the 24-finding execution audit (commit `b6e49b0`) did not cover — surfaced ~150 concrete bugs, security defects, and operational footguns across the Go API, Python worker, infrastructure, dashboard, and test suite. Several are critical: cross-tenant cache/batch leaks, an OIDC unsigned-ID-token fallback, a healer container that bypasses the docker-socket-proxy, a `/health` endpoint that always returns 200, `init.sql`↔migrations schema drift, and CI that wraps the integration suite in `|| true`. These cannot ship to a CMMC/HIPAA customer. This change bundles the remediations into a single coordinated fix so the hardening can be verified end-to-end.

## What Changes

**Go API — correctness & security**
- Add graceful shutdown with signal handling and in-flight request draining in `api/main.go`.
- Bind the OOB watchdog to `127.0.0.1` and require a shared-secret header; stop leaking DB/Redis/Temporal state publicly.
- Validate `tenantID` as a UUID before interpolating into `SET LOCAL app.current_tenant` in `api/db.go`.
- Replace plain string comparisons with `hmac.Equal` / `subtle.ConstantTimeCompare` on the SIEM webhook HMAC and the platform-ingest Bearer token.
- Harden `siem_pushback`: URL deny-list (private/link-local/metadata IPs), reject `InsecureSkipVerify` in prod, make pushback configurable per-tenant, and check the `NewRequestWithContext` error.
- Fix SSE: cap concurrent subscribers below `dbPool` capacity, add per-write deadlines, replace the `?token=` query auth with a one-shot ticket, bounds-check the `tenantID[:8]` log line, and JSON-encode all frames (no `fmt.Sprintf` into JSON).
- OIDC: **BREAKING** — fail closed when JWKS is unavailable (stop parsing unsigned ID tokens), scope JIT provisioning by issuer+external_auth_id (never by bare email), and reject RSA moduli `< 2048` bits. Propagate request context to all outbound OIDC HTTP calls.
- Auth: raise bcrypt cost ≥ 12 and always run a dummy bcrypt on email-miss to eliminate user-enumeration timing; make `checkAccountLocked` / `recordFailedLogin` a single atomic `UPDATE ... RETURNING` to close the lockout TOCTOU; GC the `authLimiter.attempts` map.
- Wrap every `go func()` (audit, feedback, pushback, OOB, apikeys) in a `recover()` helper; stop using `context.Background()` on status-update and audit writes; replace with a short detached timeout for cleanup writes.
- Fix `drainQueuedTasks` to use `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction so two API replicas cannot publish the same queued task twice.
- Fix `batch_buffer.tryBatchAlert` so the src-key / dst-key transition is a single atomic Lua script.
- Fail **closed** (not open) on Redis errors in `backpressure.checkBackpressure`; stop ignoring per-pipeline-command errors.
- Close `compliance_handlers` rows inside the loop (not via `defer` in a `for`).
- Bind parameters in `promotion_handlers` `jsonb_set` instead of `fmt.Sprintf` verdict/risk literals.
- Add tenant_id to `task_handlers.getTaskSteps` / `getTaskTimeline` and to `agent_tasks` status-failed cleanup writes.
- Make `/health` return 503 when any dependency is down. Update the test that currently cements HTTP 200.
- Replace `http.Get` in the health handler with per-call `http.Client{Timeout: 3s}`.
- Expand `admin_handlers` secret-scrubber to cover JWTs (`eyJ…`), GCP SA JSON, Azure SAS, Slack webhooks, and live Stripe keys.
- Differentiate `42P01` (relation missing) from other errors in promotion/compliance/cipher-audit queries — stop returning empty data on real failures.

**Python worker — correctness & security**
- **BREAKING**: prefix every `code_cache` key and every `smart_batcher` batch key with `tenant_id` to eliminate cross-tenant leak.
- **BREAKING**: validate `tenant_id` with `uuid.UUID(...)` before interpolating into `SET LOCAL app.current_tenant` in `worker/stages/store.py`; route `store_investigation` through the pooled connection helper (no `psycopg2.connect` per investigation).
- Fix the ingest connection leak: `_get_db()` callers currently call `conn.close()` and never return the conn to the `ThreadedConnectionPool`; wrap in a context manager.
- Add `SET LOCAL app.current_tenant` before the `agent_skills.times_used` UPDATE in `ingest.py`.
- Rewrite `tools/runner.py` so per-tool timeouts are enforced via `ThreadPoolExecutor.future.result(timeout=...)`, not post-hoc wall-clock checks; a tool that enters an infinite loop must be killable.
- Fix `llm_client.py` semaphore loop-binding: create FAST/CODE semaphores lazily per event loop.
- Fix `assess.py` IOC provenance validation: use word-boundary regex, not substring match (`192.168.1.10` must not be "confirmed" by `192.168.1.100`); harden the domain regex against ReDoS by capping input length and/or using atomic groups.
- Emit NOTIFY payloads over a **pooled** connection, not one-per-event; measure size in UTF-8 bytes, not Python `len()`.
- Wrap OTEL spans (`assess`, `govern`) in `try/finally` so spans are never leaked on exception.
- Move the circuit-breaker state into Redis (currently process-local globals with a data race across the 32-worker pool).
- Harden `input_sanitizer`: remove test-fixture patterns (`tenant-uuid-\d+`), widen `_scan_field_tail` from 200 to ~1024 chars, and stop NFKC-normalising the **stored** raw_log (forensic corruption).
- Fix `llm_gateway._get_endpoint_for_model` to route by role, not by model-name equality, so operators configuring only `ZOVARK_LLM_ENDPOINT_CODE` are not silently downgraded to the FAST endpoint.
- Fix `investigation_plans.json` plan-key lookup so exact-match is preferred over substring and ambiguity is logged.
- Fix `worker/schemas.py`: allow MITRE tactic IDs (`TA####`), and add `Field(alias="type")` to `IOCItem.ioc_type` so future strict validation does not silently drop every IOC.
- Standardise verdict enums in `output_validator.VALID_VERDICTS` to include `needs_analyst_review` and `error`.
- Remove default passwords from `worker/settings.py` and fail boot if any required secret is empty; URL-encode password values when building `database_url` / `redis_url`.
- Register `TracerProvider.shutdown()` in `worker/tracing.py`; guard lazy `init_tracing()` with a lock.
- Fix `code_cache` thundering-herd by adding TTL jitter.
- Fix `execute.py`: bound the greedy JSON extraction regex; move the blocked-string scan after AST strip so code comments do not false-positive.

**Infrastructure / schema / k8s**
- **BREAKING**: add a repo-root `.dockerignore` (exclude `.env`, `.git`, `models/`, `*.gguf`, `data/`, `dpo/`, `AUDIT_FINDINGS.md`, `archive/`) and rotate `JWT_SECRET` and any credentials that may have been baked into image layers.
- Remove the direct `docker.sock` bind from the `healer` container and route via `docker-socket-proxy` (`DOCKER_HOST=tcp://docker-socket-proxy:2375`).
- Drop `privileged: true` from `docker-socket-proxy`; add `cap_drop: [ALL]`, `read_only: true`, `security_opt: [no-new-privileges:true]`.
- **BREAKING**: bind every host port in `docker-compose.yml` that is not intended for public consumption to `127.0.0.1:` — Elasticsearch 9200, Grafana 3002, Kibana 5601, temporal-ui 8080, prometheus 9090, nginx-proxy 8080, ollama 11434, OTLP 4317/4318/13133, all exporters, and healer 8081.
- Require `GRAFANA_ADMIN_PASSWORD` from env (no hardcoded `admin/zovark`).
- Enable ES `xpack.security` in `siem-lab` profile and move it to an isolated `zovark-siem` network.
- Standardise all healthchecks on `127.0.0.1`; replace short-form `depends_on` with mapping-form + `condition: service_healthy` for dashboard, caddy, prometheus, grafana, nginx-proxy, fluent-bit.
- Run SurrealDB as a non-root user with `cap_drop: [ALL]`, `no-new-privileges`, `read_only`.
- Rewrite `worker/Dockerfile` and `api/Dockerfile` to run as a non-root `USER`, pin DuckDB by SHA, remove `docker.io`, and reorder `COPY` after `pip install` for cache reuse.
- Add a repo-root `.dockerignore` entry for `agent/Dockerfile` and re-declare the HEALTHCHECK in compose for `healer`.
- **BREAKING**: delete `init.sql` entirely (1503 lines that drift from migrations every sprint); let migrations be the single source of truth. Add a squash-migration script per release. This is the root cause of `cross_tenant_intel`/`audit_events` duplication across fresh-boot and incremental paths.
- Resolve duplicate migration prefixes (`041_`, `050_`, `051_`, `052_`) by renumbering to `070+` with a `_migration_moves.md` ledger.
- Wrap each migration in `BEGIN/COMMIT` inside `apply_migrations.sh` and add a `schema_migrations(version, applied_at)` ledger.
- Pre-create monthly partitions for `investigations` and `audit_events` through 2028; install `pg_partman` or a CronJob that creates the next month.
- Add `DROP MATERIALIZED VIEW IF EXISTS cross_tenant_intel; DROP VIEW IF EXISTS cross_tenant_public;` to migration 068 so the pgvector retirement stops silently dropping cascading views.
- **BREAKING RLS**: create a `zovark_app` Postgres role without `BYPASSRLS`; point PgBouncer at it; add `FORCE ROW LEVEL SECURITY` on every tenant-scoped table. Today's RLS is decoration because the app user owns the tables.
- Kubernetes: rename stale `zovark-mvp-*` image references to `zovark-*`; add `securityContext.runAsNonRoot / readOnlyRootFilesystem / allowPrivilegeEscalation=false / capabilities.drop:[ALL]`; add PDBs; fix the k8s `redis` deployment to use Valkey + `--requirepass` (currently the worker ships a password that the server rejects); add NetworkPolicies for redis, postgres, api, temporal, pgbouncer, dashboard; remove the LiteLLM egress allow; add the missing `postgres-init` ConfigMap reference to `kustomization.yaml` (today the k8s Postgres boots with **no** schema).
- Fix `pgbouncer` healthcheck to use `127.0.0.1` and a plain port check (stop using `pg_isready` against `localhost`).

**Dashboard**
- Replace SSE `?token=` auth with a one-shot signed ticket (critical — the JWT currently lands in nginx access logs, OTel spans, and browser history).
- Stop persisting the access token in `sessionStorage`; keep it in a module variable and rely on the httpOnly refresh cookie.
- Thread `AbortController` through every `fetch()` inside a `useEffect`; add `AbortSignal.timeout(15_000)` in `fetchWithRefresh`.
- Add CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, and a cheap `/health` to `dashboard/nginx.conf`.
- Add `Idempotency-Key` on every state-changing `client.ts` call (React StrictMode double-invokes and double-clicks currently create duplicate tasks).
- Fix `key={index}` on editable reorderable lists in `Playbooks.tsx`, `TaskDetail.tsx`.
- **BREAKING CSRF**: require `SameSite=Strict` on the refresh cookie and double-submit `X-CSRF-Token` on every non-idempotent request.
- Self-host Google Fonts (already an air-gap requirement) and drop the `preconnect`.

**Tests & CI**
- Drop `|| true` from the integration-suite step in `.github/workflows/ci.yml` (currently green on any failure).
- Pin GitHub Actions by full commit SHA; add `cache: 'pip'` and `cache: 'go-build'` to setup steps.
- Generate random Postgres passwords per CI job rather than hardcoding `hydra_dev_2026`.
- Add unit tests for `worker/stages/{analyze,execute,assess,store}.py` — today there are zero stage-level tests.
- Add a `test_rls.py` that provisions two tenants and asserts cross-tenant reads return zero rows.
- Add tests for the circuit breaker state machine, `siem_pushback` retry backoff, backpressure hard-limit 503, governance autonomy transitions, and dedup race (severity escalation + TTL expiry) against `fakeredis`, not `MagicMock`.
- Rewrite `test_alert_sanitizer.py::test_deep_nesting_does_not_crash` to assert structural invariants, not `is not None`.
- Replace `test_synthetic_login.py`'s re-implementation of `check_synthetic_login` with an import-based test against `agent/healer.py`.
- Rewrite `autoresearch/redteam/evaluate.py` to drive the real analyze/execute pipeline and compare verdict/risk to ground truth; split payloads into `train.jsonl` / `holdout.jsonl` and evaluate only on holdout.
- Rewrite `scripts/smoke_test_100.sh` with `set -euo pipefail`, `curl -fsS`, `jq`, and a non-zero exit on any FAIL.

## Capabilities

### New Capabilities
- `audit-remediation`: Single capability covering the second-round audit fixes. Specifies the hardening requirements (tenant isolation, RLS enforcement, SSE auth, OIDC JWKS enforcement, health-endpoint semantics, CI gating) that the codebase must meet on exit.

### Modified Capabilities
- None — the prior audit shipped without formal specs in `openspec/specs/`, so there is no existing spec whose requirements are changing. All new behavioral contracts are consolidated under the `audit-remediation` capability.

## Impact

- **Affected code**: `api/` (20+ files), `worker/stages/` (12 files), `worker/tools/`, `worker/schemas.py`, `worker/settings.py`, `worker/llm_client.py`, `worker/tracing.py`, `worker/events.py`, `dashboard/src/` (pages, api/client.ts, nginx.conf), `docker-compose.yml`, `docker-compose.*.yml`, `init.sql` (deleted), `migrations/` (renumbered + new), `k8s/base/**`, `worker/Dockerfile`, `api/Dockerfile`, `agent/Dockerfile`, `scripts/smoke_test_100.sh`, `scripts/apply_migrations.sh`, `scripts/backup-db.sh`, `.github/workflows/ci.yml`, `autoresearch/redteam/evaluate.py`, and the entire `worker/tests/` directory.
- **APIs**: OIDC login **breaks** for any IdP whose JWKS endpoint is unreachable (previously fell through to unsigned parsing — now fails closed). SSE stream URL changes (ticket-based auth). `/health` will now return 503 when dependencies are degraded — load balancers and health probes will drain affected replicas.
- **Dependencies**: Add `fakeredis` to `worker/tests/requirements-test.txt`; add `pg_partman` to Postgres init; add `jq` to the smoke-test images.
- **Data migration**: Deleting `init.sql` requires a squash migration so existing dev databases can fast-forward; duplicate-numbered migrations (`041_`, `050_`, `051_`, `052_`) get renumbered behind a ledger in `migrations/_migration_moves.md`.
- **Secrets**: `JWT_SECRET` must be rotated (potential leak into Docker image layers via missing `.dockerignore`); `GRAFANA_ADMIN_PASSWORD` must be provided via env going forward.
- **Operational**: `zovark_app` DB role becomes the application identity; `hydra`/`zovark` owner role is reserved for migrations only. This is the switch the existing `CLAUDE.md` "Pending Work #10" already calls out.
- **Risk**: The OIDC, CSRF, and RLS enforcement changes are the highest-risk items — each has the potential to lock users out or block writes if deployed without staged rollout. Mitigation: feature-flag each behind `ZOVARK_HARDENING_*` flags during rollout.
