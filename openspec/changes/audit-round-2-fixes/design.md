## Context

The first execution audit (`AUDIT_FINDINGS.md`, commit `b6e49b0`) shipped fixes for 24 concrete findings, focused on the most obvious execution-path defects: NATS dispatch, JWT-claim refresh, webhook secrets, SIEM-map sanitization, secret-ized `llm_key`, dead code, and branding drift. Four parallel deep-dives during this audit round covered the Go API (`api/**`), Python worker (`worker/**`), infrastructure (`docker-compose.yml`, `init.sql`, `migrations/`, `k8s/base/**`), and dashboard + CI/test surface. Those four passes surfaced ~150 additional findings, of which this change targets the ~90 concrete, actionable, and verifiable ones. Speculative findings, style nits, and items that the prior audit already addressed are explicitly out of scope.

The codebase is mid-air: the existing branch `audit/execution-fixes` has uncommitted local patches, `init.sql` + `migrations/` have drifted, and several security claims in `CLAUDE.md` (e.g. "RLS enabled on 10 tables") are decorative rather than enforced because the application's Postgres role has `BYPASSRLS`. The target customer profile (CMMC, HIPAA, air-gapped SOC) cannot accept "defense-in-depth by comment" for tenant isolation. The v3.1-hardening → master merge is Pending Item #1 in `CLAUDE.md`; the audit round should land before that merge so hardening is a single squashable window.

Stakeholders:
- SOC platform engineering — owns worker, API, governance.
- Deployment engineering — owns Docker Compose, Helm/kustomize, migrations.
- Security — owns RLS enforcement, OIDC trust, SSE/CSRF.
- QA — owns test coverage gates and CI green signal.

## Goals / Non-Goals

**Goals:**
- Close every critical and high-severity finding from the four audit passes with concrete code changes and corresponding tests.
- Make existing defense-in-depth claims **enforced** (RLS via non-bypass role, SSE auth via ticket, `/health` reflecting dependency state, CI actually failing on test failure).
- Eliminate cross-tenant leak vectors: code cache, smart batcher, SIEM pushback configuration.
- Remove schema drift between `init.sql` and `migrations/` by making migrations the single source of truth.
- Ship a `zovark_app` DB role path (Pending Item #10) and put the application behind it.
- Keep the change reversible: feature-flag the riskiest behavioural changes (OIDC fail-closed, RLS enforcement, `/health` 503) so a bad rollout can be disabled in under 60 seconds without a redeploy.

**Non-Goals:**
- Rewriting the v3 investigation pipeline, tool library, or governance engine. The audit found no correctness bugs in the v3 tool runner that require a redesign beyond the timeout-enforcement and IOC-dedup fixes.
- Replacing Temporal, Valkey, Postgres, llama-server, or the React stack.
- Migrating away from psycopg2 to asyncpg in the worker — tempting, but a multi-week job; out of scope.
- Building a new secret manager. Docker/K8s secrets + pydantic `SecretStr` remain the runtime model.
- Rewriting `init.sql` as a Helm chart — we are **deleting** it, not replacing it.
- Introducing a service mesh, OPA, or Kyverno for k8s policy enforcement. NetworkPolicy + pod securityContext is enough for the CMMC baseline.
- New features. This is a hardening and correctness sprint, full stop.

## Decisions

### D1 — `zovark_app` role + `FORCE ROW LEVEL SECURITY` over "RLS as decoration"
The current `zovark` DB owner has `BYPASSRLS` by default, so every RLS policy on tenant-scoped tables is a no-op in practice. We create a new `zovark_app` role without `BYPASSRLS`, point PgBouncer at it, and add `ALTER TABLE <t> FORCE ROW LEVEL SECURITY` to every tenant-scoped table so even the owner is subject to policy during normal operation. Migrations run under `zovark` (the owner) so DDL still works; only application connections flip to `zovark_app`.

**Alternative considered:** Relying solely on `WHERE tenant_id = $1` across every query. Rejected — the audit already found missing tenant_id clauses in `task_handlers.getTaskSteps`, `compliance_handlers`, and `backpressure.drainQueuedTasks`. We cannot police this by code review alone; we need the database to refuse the query.

**Alternative considered:** pgbouncer connection string per tenant. Rejected — PgBouncer transaction pooling rotates backends per statement, so per-tenant pools would explode the backend count.

### D2 — SSE auth via one-shot ticket, not `?token=` query param
Today `TaskList.tsx` and `TaskDetail.tsx` subscribe to `/api/v1/tasks/stream?token=<JWT>`. Query strings end up in nginx access logs, OTel HTTP spans, browser history, and `Referer` headers — the JWT leaks 4 ways. `EventSource` cannot set custom headers, so we cannot just move the token to `Authorization: Bearer`.

We introduce `POST /api/v1/auth/sse-ticket` which takes the current Bearer token and returns a short-lived (30-second), single-use ticket stored in Valkey. The dashboard then connects to `/api/v1/tasks/stream?ticket=<opaque>`. The ticket is burned on first use and auto-expires. Its scope is limited (tenant + user + "sse:read") and it is not a JWT.

**Alternative considered:** WebSocket + custom `Sec-WebSocket-Protocol` subprotocol carrying the JWT. Rejected — requires rewriting the entire SSE path on the Gin side, the React hook, and the NOTIFY → forward loop.

**Alternative considered:** Cookie-based SSE auth. Rejected — cross-site cookies under `SameSite=Strict` break when the dashboard is served from a different origin than the API in air-gap deployments.

### D3 — Fail closed on OIDC JWKS unavailability
Today `api/oidc.go:540-561` parses unsigned ID tokens with a `log.Println` warning when JWKS is unreachable. That is a signature-skipping code path. We remove it. If `initOIDC` cannot fetch JWKS at startup, the server refuses to register OIDC routes. If JWKS becomes unreachable at runtime, login returns `503 oidc_degraded` for up to `ZOVARK_OIDC_JWKS_STALE_AFTER` (default 10 minutes) using the last-good JWKS cache, then fails closed.

This is **breaking** for any deployment whose IdP JWKS URL is misconfigured. We accept the break: a SOC investigation product cannot accept unsigned identity tokens under any condition. A feature flag `ZOVARK_OIDC_ALLOW_UNSIGNED=true` (off by default; refuses to start if set in prod) is the escape hatch for staging.

### D4 — `/health` reflects dependency state, with a separate `/live`
`/health` currently always returns 200. Load balancers (and the k8s readiness probe) treat it as a liveness oracle, so a degraded replica keeps receiving traffic. We change the semantics:
- `/live` → always 200 as long as the process is accepting connections (k8s liveness probe target).
- `/health` → 200 iff DB, Redis, Temporal are all reachable; 503 otherwise. This is what load balancers and service-mesh "health" checks read.
- `/ready` → unchanged: 200 iff the process finished startup and dependencies are reachable.

The split mirrors the standard k8s probe semantics (`livenessProbe` vs `readinessProbe`) and the existing test (`errors_test.go:141-143`) must be updated to assert 503 on degradation.

### D5 — Delete `init.sql`; migrations are the single source of truth
`init.sql` has drifted from `migrations/` over seven sprints. It re-declares tables, materialised views, and IVFFLAT indexes that migrations subsequently `ALTER`/`DROP`/`RECREATE`. Compose-based dev boots run `init.sql` then migrations; k8s runs only migrations (and is in fact missing the `postgres-init` ConfigMap reference, so it runs **no** init at all). Two different schemas across environments is indefensible.

We delete `init.sql` and produce `migrations/000_squash_v3_2.sql` containing the idempotent CREATE-IF-NOT-EXISTS baseline for all tables current as of the squash point. Subsequent migrations (065+) apply on top. Fresh compose and k8s boots go through the same code path. Dev databases created before the squash keep working because the squash is a no-op on an already-populated DB (every DDL is guarded).

**Alternative considered:** Generate `init.sql` from migrations at build time. Rejected — still two paths, still drift-prone. The cleanest fix is one path.

### D6 — Tenant-scope every Redis key
Three places today share keys across tenants:
1. `worker/stages/code_cache.py` — hash of task_type + rule_name + SIEM field names. Cross-tenant cache serving.
2. `worker/stages/smart_batcher.py` — `batch:<task_type>:<source_ip>`. Cross-tenant alert mixing.
3. `api/alert_dedup.go` — already tenant-scoped, but `force_reinvestigate` bypass was not double-checking tenant.

We make `tenant_id` a mandatory prefix in every Redis namespace used by the pipeline. All helpers go through a `tenant_redis_key(tenant_id, namespace, *parts)` helper that refuses a call without a UUID tenant. This is a small, static-analysable refactor.

### D7 — Per-tool timeout via `ThreadPoolExecutor.future.result(timeout=…)`
`worker/tools/runner.py` imports `signal` but never uses it; the per-tool "timeout" is a post-hoc check after the synchronous call returns. A tool with an infinite loop today blocks its activity worker until Temporal kills the whole activity. We wrap each tool invocation in a single-worker `ThreadPoolExecutor` and call `future.result(timeout=per_tool_timeout)`, raising `TimeoutError` on the step. The IOC dedup and path_taken logging unchanged.

**Alternative considered:** `signal.SIGALRM`. Rejected — doesn't work inside Temporal activity worker threads; only the main thread can register signal handlers.

**Alternative considered:** Re-architect the tool runner as `async` and `await asyncio.wait_for`. Rejected — tools today are synchronous Python functions and an async rewrite is out of scope. The ThreadPoolExecutor path is a drop-in.

### D8 — Circuit breaker state in Redis, not module globals
`worker/stages/circuit_breaker.py` stores state in module globals with no lock and no cross-process sharing. The worker pool is sized to 32 concurrent workflows, so two workers can have divergent views of the breaker. We move state to Redis keys `zovark:cb:<name>:state`, `zovark:cb:<name>:failures`, with per-update Lua scripts to avoid TOCTOU. The in-process cache is kept but is read-through with a 1-second TTL to avoid a Redis round-trip on every health check.

### D9 — `LogInto(tenant_id)` validator as a reusable helper
Three files today interpolate `tenant_id` into `SET LOCAL app.current_tenant = '<id>'` via `fmt.Sprintf` or f-string: `api/db.go`, `worker/stages/store.py`, and `worker/stages/ingest.py`. Each one is a latent SQL injection if `tenant_id` ever becomes user-controlled. We add a `validateTenantID(string) error` helper (Go + Python) that calls `uuid.Parse` / `uuid.UUID(...)` and refuses invalid input. Every call site must go through it. A lint rule / `golangci-lint custom` check can enforce this going forward; for now we do it by code review + the `grep` check in tasks.md.

### D10 — Tool timeouts & span lifetimes via context managers
OTEL spans in `assess.py`, `govern.py`, and the tool runner today are `.end()`'d only on the happy path. We adopt the pattern `span = tracer.start_as_current_span("x"); try: ... finally: span.end()` (actually use `with tracer.start_as_current_span(...)` which handles cleanup). This eliminates every leaked-span finding from the audit in one refactor.

### D11 — `fakeredis` for unit tests; no more `MagicMock` for Redis
`worker/tests/test_dedup.py` mocks Redis with `MagicMock`, which tests the SDK surface rather than the dedup algorithm. We add `fakeredis` to the test-only requirements file and rewrite the dedup tests against it. The circuit-breaker state, batch buffer, and code cache tests are also rewritten on `fakeredis`. Integration tests against a real Redis run in the existing `docker-compose.test.yml` CI job and do **not** use `fakeredis`.

### D12 — Feature flags for staged rollout
Four behavioural changes can lock users out if deployed without a rollback path: RLS enforcement (D1), OIDC fail-closed (D3), `/health` 503 semantics (D4), and CSRF double-submit (dashboard). Each goes behind a `ZOVARK_HARDENING_*` flag that defaults to **off** in the first PR of this change and is flipped **on** in a follow-up PR once smoke tests pass:
- `ZOVARK_HARDENING_RLS_FORCE=true` → application connects as `zovark_app` with `FORCE RLS`.
- `ZOVARK_HARDENING_OIDC_JWKS_REQUIRED=true` → OIDC refuses to start without JWKS and never accepts unsigned tokens.
- `ZOVARK_HARDENING_HEALTH_503=true` → `/health` returns 503 when a dependency is down.
- `ZOVARK_HARDENING_CSRF=true` → dashboard + API enforce `X-CSRF-Token` double-submit.

Each flag is removed in the PR that archives this change (flags are ratchets, not permanent config).

## Risks / Trade-offs

- **[Risk] OIDC fail-closed locks out customers with flaky JWKS endpoints.** → Mitigation: 10-minute JWKS staleness window, cached last-good keys, `ZOVARK_HARDENING_OIDC_JWKS_REQUIRED` flag defaulting to off in PR-1 and on in PR-2 after staging verification.
- **[Risk] `zovark_app` role + `FORCE RLS` breaks any query that forgot to `SET LOCAL app.current_tenant`.** → Mitigation: every test suite runs under the new role; a pre-merge grep sweep flags any code path that opens a non-tenant transaction; `ZOVARK_HARDENING_RLS_FORCE` flag for staged rollout.
- **[Risk] Deleting `init.sql` breaks fresh-boot dev environments that depend on non-migrated seed data.** → Mitigation: seed data (tenants, users, default agent_skills) moves into `migrations/000_squash_v3_2.sql` guarded by `NOT EXISTS` checks. `scripts/bootstrap_dev.sh` is updated to call the migrator instead of piping `init.sql`.
- **[Risk] Binding ports to 127.0.0.1 breaks remote-accessed dev dashboards and Grafana.** → Mitigation: `docker-compose.override.yml.example` documents how to expose services for laptop-accessed dev; CI and prod use the locked-down base file. Actual customer deployments go through Caddy/nginx TLS termination.
- **[Risk] Rewriting `autoresearch/redteam/evaluate.py` to drive the real pipeline slows the AutoResearch loop from seconds to minutes.** → Mitigation: acceptable; a slow but honest fitness function beats a fast liar. The generator is budget-bounded per cycle.
- **[Risk] CSRF double-submit breaks any non-browser client that calls state-changing endpoints.** → Mitigation: API-key callers (header `X-API-Key`) skip CSRF entirely; only JWT + cookie callers are subject to it. Documented in `docs/SIEM_INTEGRATION.md`.
- **[Risk] The test-gap closure (unit tests for analyze/execute/assess/store) is large — ~30 new tests.** → Mitigation: scope it to happy path + one error path per stage; exhaustive coverage is a follow-up ticket.
- **[Risk] Schema squash migration misses a rare column defined in `init.sql` but never in `migrations/`.** → Mitigation: dump existing dev and staging DBs with `pg_dump --schema-only` and diff against `000_squash_v3_2.sql` before the squash lands. Run the squash in a disposable DB in CI.
- **[Risk] `code_cache` tenant-scoping invalidates every existing cache entry on first deploy.** → Mitigation: acceptable — the cache is a 24-hour warm store, not authoritative data. The thundering-herd after flush is handled by the new TTL jitter.
- **[Trade-off] `fakeredis` in unit tests drifts from real Valkey semantics.** → Accepted: integration tests in `docker-compose.test.yml` still run against real Valkey.

## Migration Plan

1. **PR-1 (non-breaking fixes, ~60 findings)**: All Go API `recover()` wrappers, context propagation, SQL tenant filters, SSE backpressure, secret scrubber expansion, `fmt.Sprintf` → parameterised SQL, `http.Client` timeouts, `drainQueuedTasks` `FOR UPDATE SKIP LOCKED`, `batch_buffer` atomic Lua, `backpressure` fail-closed. Worker: connection-leak fix, per-tool timeout via `ThreadPoolExecutor`, tenant-scoped Redis keys, NFKC scope fix, `llm_client` semaphore fix, span `try/finally`, schema fixes, circuit breaker Redis. Infra: `.dockerignore`, `127.0.0.1:` port bindings, `docker-socket-proxy` caps, healer routed via proxy, Dockerfile non-root users, duplicate migration renumbering, squash migration created (but not activated), `cross_tenant_intel` drop in mig 068. Dashboard: `AbortController`, fetch timeout, nginx security headers, `Idempotency-Key`, key-by-ID on reorderable lists, Google Fonts self-hosted, access token out of sessionStorage. CI: drop `|| true`, pin actions by SHA, add cache, random per-job passwords, `smoke_test_100.sh` rewrite, tests for stages/RLS/circuit-breaker/pushback/backpressure/governance.
2. **PR-2 (feature-flagged breaks)**: Introduce `ZOVARK_HARDENING_RLS_FORCE`, `ZOVARK_HARDENING_OIDC_JWKS_REQUIRED`, `ZOVARK_HARDENING_HEALTH_503`, `ZOVARK_HARDENING_CSRF`. Land all four implementations in code, default all to `false`. Ship to staging.
3. **PR-3 (activation)**: Flip all four flags to `true` in staging, run the full smoke corpus (515 alerts) and red-team corpus, verify no regressions, then flip in prod. Remove the flags in the commit that archives this change.
4. **PR-4 (schema squash cutover)**: Activate `migrations/000_squash_v3_2.sql` and delete `init.sql`. Run in a disposable dev DB first; diff the resulting schema against a `pg_dump --schema-only` of the current dev DB; merge once the diff is empty. Update `scripts/bootstrap_dev.sh` and k8s `postgres-init` ConfigMap reference.
5. **Rollback strategy**: Each PR reverts cleanly. The feature flags in PR-2 are the primary rollback lever: flip them back off without redeploying code. The schema squash (PR-4) is the only non-reversible piece; gate it behind a manual release-engineer signoff and keep the pre-squash dev DB snapshot for 30 days.

## Open Questions

- **Q1**: Should we introduce a `golangci-lint` custom rule to enforce that every `beginTenantTx` call site validates `tenantID` with `uuid.Parse`, or is a one-time grep sufficient? → **Recommendation**: one-time grep for PR-1, lint rule as a follow-up.
- **Q2**: The CSRF double-submit design only covers JWT + cookie callers. API-key callers currently have no CSRF — is that acceptable for the CMMC audit? → **Needs confirmation from Security lead.**
- **Q3**: Do we want to keep `/ready` as a separate endpoint from the new `/health` once `/health` reflects dependency state, or alias them? → **Recommendation**: keep both; `/ready` = "startup done", `/health` = "dependencies OK", `/live` = "process alive". Standard three-probe pattern.
- **Q4**: The squash migration (`000_squash_v3_2.sql`) baseline — do we squash up to the current HEAD (migration 068) or leave 065-068 as delta migrations on top of the squash? → **Recommendation**: squash through 068; the post-squash migrations resume at 100+.
- **Q5**: Does `zovark_app` need `GRANT EXECUTE` on the `current_tenant_id()` function created in migration 035? → **Yes** — migration adds the grant. Verify in PR-4.
- **Q6**: For the OIDC `jitProvision` tenant scoping, do we key on `iss + sub` or `iss + external_auth_id`? The existing `users.external_auth_id` column stores the IdP `sub`, so `iss + external_auth_id` is the natural composite unique key. → **Recommendation**: `iss + external_auth_id`, add a partial unique index, and reject logins that do not match any existing user (no JIT creation by email).
