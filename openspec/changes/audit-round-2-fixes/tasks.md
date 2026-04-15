## 1. PR-1 — Go API Correctness & Hardening

- [x] 1.1 Add graceful shutdown to `api/main.go` with `signal.NotifyContext`, `http.Server.Shutdown(ctx)`, and close `drainCancel` before `server.Shutdown`
- [x] 1.2 Bind the OOB watchdog in `api/oob.go` to `127.0.0.1:9091` and require a shared-secret `X-Zovark-OOB-Token` header
- [x] 1.3 Add `validateTenantID(string) error` helper in `api/db.go`; call `uuid.Parse` before interpolating into `SET LOCAL app.current_tenant`; replace the `fmt.Sprintf` with the validated path
- [x] 1.4 Replace plain `==` HMAC comparison in `api/siem.go` webhook handler with `hmac.Equal`
- [x] 1.5 Replace the Bearer token comparison in `api/handlers/platform_ingest.go` with `subtle.ConstantTimeCompare`
- [x] 1.6 Harden `api/siem_pushback.go`: add `isPrivateOrLinkLocal(url) bool` check against `10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16`, `::1`, `fe80::/10`; reject `InsecureSkipVerify` in prod; check `http.NewRequestWithContext` error; wrap `triggerPushbackFromNotify` in `defer recover()`
- [ ] 1.7 Make SIEM push-back per-tenant — add `tenant_id` column to the push-back config reader and emit only to the owning tenant's configured URL
- [x] 1.8 Cap concurrent SSE subscribers in `api/sse.go` with a semaphore sized at `min(dbPool.MaxConns/3, 200)`; return `503 sse_capacity_exceeded` past the limit
- [x] 1.9 Add 5-second per-write deadline in `api/sse.go` streaming loop; bounds-check `tenantID[:8]`; replace `fmt.Sprintf` data frames with `json.Marshal`
- [ ] 1.10 Replace SSE `?token=` query auth: implement `POST /api/v1/auth/sse-ticket` returning a 30-second single-use ticket stored in Valkey; make `middleware.go` reject SSE without a ticket
- [ ] 1.11 OIDC: remove the unsigned-ID-token fallback path in `api/oidc.go:540-561`; fail closed if `jwks.Get` returns empty; add RSA modulus size check `>= 2048`
- [ ] 1.12 OIDC: scope `jitProvision` by `(iss, external_auth_id)`; add `UNIQUE INDEX users_oidc_composite ON users (external_auth_id) WHERE auth_provider != 'local'` via a new migration; reject email-only matches
- [ ] 1.13 OIDC: propagate `c.Request.Context()` to every outbound `http.Client` call in `api/oidc.go` (discovery, JWKS refresh, token exchange)
- [x] 1.14 Raise bcrypt cost to 12 in `api/auth.go`; always run a dummy bcrypt compare on email-miss to eliminate timing oracle
- [x] 1.15 Rewrite `checkAccountLocked` / `recordFailedLogin` in `api/security.go` as a single atomic `UPDATE ... RETURNING failed_login_attempts, locked_until`; propagate request context
- [x] 1.16 Add a sweep goroutine in `api/security.go` that GCs `authLimiter.attempts` entries every 60 seconds
- [x] 1.17 Write `safeGoroutine(name string, fn func(context.Context))` helper in `api/main.go`; wrap every `go func()` in `audit`, `feedback`, `siem_pushback`, `oob`, `apikeys`, `oidc`, `sse` with it
- [x] 1.18 Replace `context.Background()` on status-update and audit-cleanup writes with `context.WithTimeout(context.Background(), 5*time.Second)` so cancelled request contexts don't swallow failure-path writes
- [x] 1.19 Add `REFRESH MATERIALIZED VIEW CONCURRENTLY` debounce ticker in `api/feedback.go` (one refresh per 30s, not per submit)
- [x] 1.20 Rewrite `api/backpressure.go:drainQueuedTasks` to use `FOR UPDATE SKIP LOCKED` inside a transaction; update status in the same transaction; add tenant_id to the query
- [x] 1.21 Fail closed (not open) on Redis errors in `api/backpressure.go:checkBackpressure`; check per-pipeline-command errors
- [x] 1.22 Rewrite `api/batch_buffer.go:tryBatchAlert` src→dst transition as a single atomic Lua script
- [x] 1.23 Fix `api/compliance_handlers.go` rows leak: move `rows.Close()` from `defer` inside the `for` loop to an explicit close at the end of the inner block
- [x] 1.24 Replace `fmt.Sprintf` verdict/risk literals in `api/promotion_handlers.go` `jsonb_set` with bound `$N::text` / `$N::int` parameters
- [x] 1.25 Add `AND tenant_id = $2` (or equivalent RLS-compatible predicate) to `getTaskStepsHandler`, `getTaskTimelineHandler`, and every `SELECT` in `api/task_handlers.go` that omits it
- [x] 1.26 Replace `http.Get` in `api/handlers.go` health handler with per-call `http.Client{Timeout: 3 * time.Second}`
- [x] 1.27 Differentiate `42P01` (relation missing) from other pg errors in `api/promotion_handlers.go`, `api/compliance_handlers.go`, `api/cipher_audit_handlers.go`; return 500 on real failures
- [x] 1.28 Expand `api/admin_handlers.go:secretPatterns` to match `eyJ[\w-]+\.[\w-]+\.[\w-]+` (JWT), GCP SA JSON (`"private_key":`), Azure SAS (`sig=`), Slack webhook URLs (`hooks.slack.com/services/`), and Stripe live keys (`sk_live_[0-9a-zA-Z]{24,}`)
- [x] 1.29 Check error return of `redisClient.Del` in `api/alert_dedup.go` retry path; log and escalate on failure
- [x] 1.30 Check error return of `redisClient.SetEx` in `api/oidc.go` state writes; fail fast on Redis error
- [x] 1.31 Bounds-check `tenantID[:8]` uses across `api/` (grep for `tenantID\[:`) and replace with `firstN(tenantID, 8)` helper
- [x] 1.32 Fix `api/totp.go` to propagate `c.Request.Context()` through all DB calls; log internal decoding errors in the verification loop

## 2. PR-1 — Worker Pipeline Correctness

- [x] 2.1 Fix `worker/stages/ingest.py` connection leak: wrap `_get_db()` in a `@contextmanager` that calls `pool.putconn(conn)` in `finally`; update every caller in `ingest.py`, `analyze.py`, `assess.py`, `store.py`
- [x] 2.2 Add `validate_tenant_id(tenant_id: str) -> str` helper in `worker/stages/tenant.py`; rewrite every `SET LOCAL app.current_tenant = '...'` f-string to go through it; validate with `uuid.UUID(...)`
- [x] 2.3 Route `worker/stages/store.py:store_investigation` through the pooled connection helper; remove the per-invocation `psycopg2.connect`
- [x] 2.4 Add `SET LOCAL app.current_tenant` before the `agent_skills.times_used` UPDATE in `worker/stages/ingest.py`
- [x] 2.5 Rewrite `worker/tools/runner.py` per-tool timeout via `concurrent.futures.ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=per_tool_timeout)`; raise `StepTimeoutError` on timeout; store the failure in `step_results[step_idx]`
- [x] 2.6 Remove the unused `import signal` in `worker/tools/runner.py`
- [ ] 2.7 Store `(tool_name, result)` tuples in a parallel dict in `worker/tools/runner.py` so risk-floor attribution survives skipped conditional branches
- [x] 2.8 Lazy-construct `_fast_semaphore` and `_code_semaphore` inside `llm_request` in `worker/llm_client.py`; rebind when the event loop changes
- [x] 2.9 Fix `worker/stages/llm_gateway.py:_get_endpoint_for_model` to route by role (stage/role argument), not by model-name equality; add a test covering the `MODEL_FAST==MODEL_CODE` default case
- [x] 2.10 Prefix `code_cache` Redis key with `tenant_id` in `worker/stages/code_cache.py:get_alert_signature` and make `tenant_id` a required argument
- [x] 2.11 Add TTL jitter (`CACHE_TTL + random.randint(0, 3600)`) to `worker/stages/code_cache.py`
- [x] 2.12 Prefix batch key with `tenant_id` in `worker/stages/smart_batcher.py:_batch_key`; require tenant_id at the call site
- [x] 2.13 Fall back from `source_ip=unknown` to `hostname` or `rule_name` in `worker/stages/smart_batcher.py` to avoid cross-alert pollution
- [x] 2.14 Move `worker/stages/circuit_breaker.py` state into Redis with Lua scripts for atomic updates; keep a 1-second in-process read-through cache
- [x] 2.15 Fix `worker/stages/assess.py` IOC provenance validation: use word-boundary regex `r'\b' + re.escape(value) + r'\b'` for IPs, usernames, hashes, CVEs, domains, URLs
- [x] 2.16 Harden `worker/stages/assess.py` domain regex: cap `combined_text` to 16 KB before applying the pattern; switch to `regex` library with atomic groups or rewrite as a non-backtracking tokenizer
- [x] 2.17 Wrap all OTEL spans in `worker/stages/assess.py` and `worker/stages/govern.py` with `with tracer.start_as_current_span(...)` context managers so spans are not leaked on exception
- [x] 2.18 Split broad `except (ImportError, Exception)` clauses in `worker/stages/assess.py:707` into distinct `except ValidationError` and `except ImportError` handlers
- [x] 2.19 Harden `worker/stages/input_sanitizer.py`: NFKC-normalise only the scan copy, keep the original `raw_log` for storage and downstream emission
- [x] 2.20 Remove `tenant-uuid-\d+` / `other-tenant-uuid-\d+` patterns from production `INJECTION_PATTERNS`; move to `worker/tests/fixtures/sanitizer_test_patterns.py`
- [x] 2.21 Widen `worker/stages/input_sanitizer.py:_scan_field_tail` from `value[-200:]` to `value[-1024:]`
- [x] 2.22 Fix `worker/events.py`: measure payload size in UTF-8 bytes (`len(payload.encode("utf-8"))`); reuse a pooled connection for NOTIFY emission (or asyncpg shared connection)
- [ ] 2.23 Fix `worker/stages/store.py:store_investigation` NOTIFY payload truncation to respect the 7900-byte Postgres limit after UTF-8 encoding
- [x] 2.24 Fix `worker/schemas.py` MITRE regex to accept `TA\d{4}` (tactic IDs) in addition to `T\d{4}(\.\d{3})?`
- [x] 2.25 Add `Field(alias="type")` to `worker/schemas.py:IOCItem.ioc_type`
- [x] 2.26 Add `needs_analyst_review` and `error` to `worker/stages/output_validator.py:VALID_VERDICTS`
- [x] 2.27 Remove default hard-coded passwords from `worker/settings.py` (`hydra_dev_2026`, `hydra-redis-dev-2026`, `change-me-surreal`); raise on empty required secret at startup
- [x] 2.28 URL-encode `db_password` and `redis_password` in `worker/settings.py:database_url` / `redis_url` properties via `urllib.parse.quote`
- [x] 2.29 Guard `worker/tracing.py:get_tracer()` lazy init with a `threading.Lock`; register `atexit.register(provider.shutdown)` alongside the log-provider flush
- [x] 2.30 Fix `worker/stages/analyze.py` plan-key lookup: exact match → alias map → longest-prefix substring; log ambiguity
- [x] 2.31 Narrow `worker/stages/analyze.py:849` `except (RuntimeError, httpx.TimeoutException, httpx.ConnectError, Exception)` — drop the redundant `Exception`
- [x] 2.32 Bound `worker/stages/execute.py:194` greedy JSON regex at `stdout[:65536]` or use `json.JSONDecoder().raw_decode`
- [x] 2.33 Move `worker/stages/execute.py:_check_blocked_strings` after the AST strip so comments and docstrings do not false-positive
- [x] 2.34 Remove unused `import time` in `worker/stages/investigation_workflow.py`

## 3. PR-1 — Infrastructure, Schema, Dockerfiles

- [x] 3.1 Add repo-root `.dockerignore` excluding `.env*`, `.git`, `models/`, `*.gguf`, `data/`, `dpo/`, `AUDIT_FINDINGS.md`, `archive/`, `state/`, `overnight_report_*`
- [ ] 3.2 Rotate `JWT_SECRET` in the deployment runbook and force a session reset
- [x] 3.3 Rewrite `worker/Dockerfile`: `USER 1000:1000`, remove `docker.io`, reorder `COPY` after `pip install`, pin `python:3.11-slim-bookworm@sha256:…`, add `HEALTHCHECK`
- [x] 3.4 Rewrite `api/Dockerfile`: pin DuckDB zip by SHA256, verify before unpack, `USER 65532:65532`, add `HEALTHCHECK`
- [x] 3.5 Re-declare `HEALTHCHECK` for `healer` service in `docker-compose.yml`
- [x] 3.6 Remove `/var/run/docker.sock` bind from `healer` service; add `DOCKER_HOST=tcp://docker-socket-proxy:2375`; add `depends_on: {docker-socket-proxy: {condition: service_healthy}}`
- [x] 3.7 Drop `privileged: true` from `docker-socket-proxy` service; add `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `read_only: true`
- [x] 3.8 Run `surrealdb` as upstream non-root user; add `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`
- [x] 3.9 Bind every host port in `docker-compose.yml` to `127.0.0.1:` for `temporal-ui`, `dashboard`, `grafana`, `prometheus`, `kibana`, `elasticsearch`, `nginx-proxy`, `temporal-exporter`, `worker-metrics`, `redis-exporter`, `postgres-exporter`, `ollama`, `healer`, OTLP `4317/4318/13133`
- [x] 3.10 Document `docker-compose.override.yml.example` for laptop-accessed dev with `0.0.0.0:` bindings; gitignore the real override
- [x] 3.11 Replace `GF_SECURITY_ADMIN_PASSWORD=zovark` with `${GRAFANA_ADMIN_PASSWORD:?required}` in `docker-compose.yml`
- [x] 3.12 Enable `xpack.security.enabled=true` in `docker-compose.yml` siem-lab profile; move the profile to an isolated `zovark-siem` bridge network
- [x] 3.13 Fix `docker-compose.yml` `pgbouncer` healthcheck: `pg_isready -h 127.0.0.1 -p 5432 -U zovark`
- [x] 3.14 Standardise healthchecks on `127.0.0.1` across all services (`api`, `signoz`, `inference`, `dashboard`, `minio`, `elasticsearch`, `kibana`, `pgbouncer`)
- [ ] 3.15 Convert short-form `depends_on` to mapping form with `condition: service_healthy` for `dashboard→api`, `caddy→{api,dashboard}`, `fluent-bit→api`, `prometheus→temporal-exporter`, `grafana→prometheus`, `nginx-proxy→juice-shop`
- [x] 3.16 Add `restart: unless-stopped`, `cpus: 0.5`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `read_only: true` to `healer`
- [x] 3.17 Bind signoz OTEL `pprof` extension to `127.0.0.1:1777` and remove host port mapping for `13133`
- [x] 3.18 Rewrite `scripts/backup-db.sh` with `set -euo pipefail`, `PIPESTATUS` check after `pg_dump | gzip`, `: "${POSTGRES_PASSWORD:?required}"`, remove `2>/dev/null`
- [ ] 3.19 Rewrite `scripts/apply_migrations.sh` with `schema_migrations(version, applied_at)` ledger, wrap each migration in `BEGIN/COMMIT`, skip already-applied, fail hard on checksum mismatch
- [ ] 3.20 Renumber duplicate migrations (`041_`, `050_`, `051_`, `052_`) to unique `070+` numbers; add `migrations/_migration_moves.md` ledger documenting the rename
- [x] 3.21 Add `DROP MATERIALIZED VIEW IF EXISTS cross_tenant_intel; DROP VIEW IF EXISTS cross_tenant_public;` to the top of `migrations/068_ticket2_surreal_graph_pgvector_retirement.sql`
- [x] 3.22 Create `migrations/069_partition_maintenance.sql` that installs `pg_partman` (or a plain `create_next_month_partition()` function) and pre-creates monthly partitions through 2028 for `investigations` and `audit_events`
- [ ] 3.23 Add a k8s `CronJob` calling the partition-maintenance function daily
- [ ] 3.24 Create `migrations/000_squash_v3_2.sql` as the idempotent baseline generated from the current dev schema; gate behind PR-4
- [ ] 3.25 Rename `k8s/base/api/deployment.yaml` and `k8s/base/worker/deployment.yaml` image tags from `zovark-mvp-*:latest` to `zovark-*:v3.2.1`; set `imagePullPolicy: IfNotPresent`
- [ ] 3.26 Add `securityContext` (`runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`) to every k8s Deployment and StatefulSet
- [ ] 3.27 Add `PodDisruptionBudget` (`minAvailable: 1`) to `api`, `worker`, `dashboard`
- [ ] 3.28 Replace k8s `redis` Deployment with `valkey/valkey:7-alpine`, add `--requirepass`, a PVC, and matching `REDIS_URL` env on worker/api
- [ ] 3.29 Add `NetworkPolicy` manifests for `redis`, `postgres`, `api`, `temporal`, `pgbouncer`, `dashboard`; whitelist only the expected peers
- [ ] 3.30 Remove the LiteLLM egress allow from `k8s/base/worker/networkpolicy.yaml`; add egress to `zovark-inference` and (optionally) OpenAI for cloud-tier customers
- [ ] 3.31 Add the missing `postgres-init` ConfigMap to `k8s/base/postgres/kustomization.yaml` (generated from `migrations/000_squash_v3_2.sql`), or remove the mount entirely and rely on the migrator Job
- [ ] 3.32 Add `securityContext` and `fsGroup` to `k8s/base/postgres/statefulset.yaml`; enforce `sslmode=require` in app connection strings
- [ ] 3.33 Pin all GitHub Actions in `.github/workflows/*.yml` by full commit SHA
- [x] 3.34 Add `cache: 'pip'` and Go module cache to `.github/workflows/ci.yml` setup steps
- [ ] 3.35 Replace hardcoded CI Postgres password with `POSTGRES_PASSWORD=$(openssl rand -hex 16)` per job
- [x] 3.36 Drop `|| true` from the integration-test step in `.github/workflows/ci.yml`

## 4. PR-1 — Dashboard

- [ ] 4.1 Implement `dashboard/src/api/sseTicket.ts` that calls `POST /api/v1/auth/sse-ticket` and caches the 30-second ticket
- [ ] 4.2 Update `dashboard/src/pages/TaskList.tsx` and `TaskDetail.tsx` to connect via `/api/v1/tasks/stream?ticket=<ticket>` instead of `?token=<jwt>`
- [ ] 4.3 Remove `sessionStorage.setItem('zovark_token', token)` from `dashboard/src/api/client.ts`; hold the access token only in a module variable; rely on the httpOnly refresh cookie for recovery
- [x] 4.4 Thread `AbortController` through every `fetch()` inside `useEffect` across `dashboard/src/pages/*.tsx`; call `controller.abort()` in the effect cleanup
- [x] 4.5 Add `AbortSignal.timeout(15_000)` in `dashboard/src/api/client.ts:fetchWithRefresh`
- [x] 4.6 Add `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, HSTS, and `location = /health { return 200; }` to `dashboard/nginx.conf`
- [x] 4.7 Self-host the Google Fonts under `dashboard/public/fonts/`; drop the `<link rel="preconnect">` from `dashboard/index.html`
- [ ] 4.8 Generate a `crypto.randomUUID()` idempotency key per user action in every state-changing call in `dashboard/src/api/client.ts` (`createTask`, `updateTask`, `submitFeedback`, `approveBatch`, etc.); send as `Idempotency-Key` header
- [x] 4.9 Replace `key={index}` with `key={step.id ?? crypto.randomUUID()}` in `dashboard/src/pages/Playbooks.tsx`, `TaskDetail.tsx:832,846,869,991,1013,1041`
- [ ] 4.10 Wire up `SameSite=Strict` on the refresh cookie in `api/auth.go`; add `X-CSRF-Token` double-submit verification in `api/middleware.go` guarded by `ZOVARK_HARDENING_CSRF` flag
- [x] 4.11 Add `build: { sourcemap: false }` to `dashboard/vite.config.ts`
- [ ] 4.12 Consolidate `dashboard/src/api/client.ts` SSE endpoint helper into a single `openSseStream(path, onMessage, onError)` that always threads the ticket

## 5. PR-1 — Tests & CI

- [ ] 5.1 Add `fakeredis` to `worker/tests/requirements-test.txt`
- [ ] 5.2 Write `worker/tests/test_analyze.py` with (a) saved-plan fast path, (b) LLM tool-selection fallback path, (c) plan-alias resolution ambiguity
- [ ] 5.3 Write `worker/tests/test_execute.py` with (a) happy-path tool chain, (b) per-tool timeout via `ThreadPoolExecutor`, (c) conditional branch skip correctness, (d) AST blocked-module rejection
- [ ] 5.4 Write `worker/tests/test_assess.py` with (a) verdict threshold transitions, (b) IOC provenance word-boundary, (c) suppression detection true/false positives, (d) span `try/finally` cleanup
- [ ] 5.5 Write `worker/tests/test_govern.py` covering autonomy slider transitions (manual → semi → auto)
- [ ] 5.6 Write `worker/tests/test_store.py` with (a) synchronous_commit assertion, (b) NOTIFY payload truncation in UTF-8 bytes, (c) tenant_id validation
- [ ] 5.7 Write `worker/tests/test_rls.py` that provisions two tenants via the `zovark_app` role and asserts cross-tenant reads return zero rows
- [ ] 5.8 Rewrite `worker/tests/test_dedup.py` against `fakeredis`: add concurrent `register_alert` race test, severity escalation test, TTL expiry test
- [ ] 5.9 Write `worker/tests/test_circuit_breaker.py` covering open/half-open/closed transitions with Redis-backed state
- [ ] 5.10 Write `worker/tests/test_siem_pushback.py` covering retry backoff, SSRF deny-list, per-tenant routing
- [ ] 5.11 Write `worker/tests/test_backpressure.py` covering hard-limit 503 response and fail-closed on Redis outage
- [x] 5.12 Rewrite `worker/tests/test_alert_sanitizer.py::test_deep_nesting_does_not_crash` to assert structural invariants rather than `is not None`
- [x] 5.13 Rewrite `worker/tests/test_synthetic_login.py` to `import healer` and call `healer.check_synthetic_login(...)` with patched env
- [x] 5.14 Replace `time.sleep(0.5)` in `worker/tests/test_signoz_telemetry.py:111` with `tracer_provider.force_flush(timeout_millis=5000)`
- [ ] 5.15 Update `api/errors_test.go:141-143` to assert HTTP 503 on dependency outage under `ZOVARK_HARDENING_HEALTH_503=true`
- [ ] 5.16 Rewrite `autoresearch/redteam/evaluate.py` to drive the real analyze/execute/assess pipeline via the API and score against ground-truth labels
- [ ] 5.17 Split `autoresearch/redteam/payloads/` into `train.jsonl` and `holdout.jsonl`; evaluate fitness only on holdout
- [x] 5.18 Replace deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)` in `autoresearch/redteam/evaluate.py`
- [x] 5.19 Rewrite `scripts/smoke_test_100.sh` with `set -euo pipefail`, `curl -fsS`, `jq -r '.verdict'`, and `exit $((FAIL > 0 ? 1 : 0))`
- [ ] 5.20 Add `tests/integration/test_e2e_pipeline.py` that submits one attack and one benign alert and asserts verdict + risk through the full pipeline under the `zovark_app` role

## 6. PR-2 — Feature-flagged behavioural changes

- [ ] 6.1 Introduce `ZOVARK_HARDENING_RLS_FORCE` env flag; when true, API and worker connect as `zovark_app` and every tenant-scoped table is `FORCE RLS`
- [ ] 6.2 Introduce `ZOVARK_HARDENING_OIDC_JWKS_REQUIRED` env flag; when true, OIDC refuses to start without JWKS and never accepts unsigned tokens
- [ ] 6.3 Introduce `ZOVARK_HARDENING_HEALTH_503` env flag; when true, `/health` returns 503 on dependency failure
- [ ] 6.4 Introduce `ZOVARK_HARDENING_CSRF` env flag; when true, dashboard and API enforce `X-CSRF-Token` double-submit on non-idempotent requests
- [ ] 6.5 Create `migrations/070_zovark_app_role.sql` that creates the `zovark_app` role without `BYPASSRLS`, grants the required privileges, and adds `FORCE ROW LEVEL SECURITY` on every tenant-scoped table
- [ ] 6.6 Update `docker-compose.yml` PgBouncer config to offer both the legacy `zovark` owner and the new `zovark_app` role; application services switch based on `ZOVARK_HARDENING_RLS_FORCE`
- [ ] 6.7 Add CSRF token issuance to `api/auth.go` login handler (set `X-CSRF-Token` cookie + return in response); add double-submit verification middleware
- [ ] 6.8 Update `dashboard/src/api/client.ts` to read the CSRF token from the cookie and send it on every non-GET request
- [ ] 6.9 Verify all feature flags default to `false` in PR-2 shipped manifests and env templates

## 7. PR-3 — Staged activation

- [ ] 7.1 Flip `ZOVARK_HARDENING_RLS_FORCE=true` in staging; run the full 515-alert corpus and verify zero RLS-related failures
- [ ] 7.2 Flip `ZOVARK_HARDENING_OIDC_JWKS_REQUIRED=true` in staging; run a full OIDC login cycle against the configured IdP
- [ ] 7.3 Flip `ZOVARK_HARDENING_HEALTH_503=true` in staging; intentionally stop Redis and verify the load balancer drains the replica
- [ ] 7.4 Flip `ZOVARK_HARDENING_CSRF=true` in staging; run the dashboard end-to-end smoke test
- [ ] 7.5 Flip all four flags in production after staging validation
- [ ] 7.6 Remove the four feature flags in the commit that archives this change

## 8. PR-4 — Schema squash cutover

- [ ] 8.1 Diff `migrations/000_squash_v3_2.sql` against `pg_dump --schema-only` of the current dev DB; iterate until the diff is empty
- [ ] 8.2 Run the squash in a disposable CI Postgres and apply all post-squash migrations; verify schema equivalence with dev
- [ ] 8.3 Delete `init.sql` from the repository
- [ ] 8.4 Update `scripts/bootstrap_dev.sh` to call `scripts/apply_migrations.sh` instead of piping `init.sql`
- [ ] 8.5 Update `k8s/base/postgres/statefulset.yaml` to remove the `configMap.name: postgres-init` mount or point it at a generated ConfigMap from the squash
- [ ] 8.6 Snapshot the pre-squash dev DB and archive it for 30 days
- [ ] 8.7 Merge PR-4 only after a release-engineer signoff

## 9. Release & archive

- [ ] 9.1 Run the full 515-alert smoke corpus against a fresh docker-compose stack using PR-1+PR-2+PR-3 code with all hardening flags on
- [ ] 9.2 Run `autoresearch/redteam/` and `autoresearch/templates/` loops; verify no regression in the template fitness scores
- [ ] 9.3 Run `zvadmin diagnose`; confirm all 8 checks pass
- [ ] 9.4 Update `CLAUDE.md` "Pending Work" section to mark items #1 (v3.1-hardening merge) and #10 (`zovark_app` role) complete
- [ ] 9.5 Archive this change via `/opsx:archive audit-round-2-fixes`
