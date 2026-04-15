## ADDED Requirements

### Requirement: Tenant isolation at the database layer
The application SHALL connect to PostgreSQL as a role without `BYPASSRLS` and every tenant-scoped table SHALL be marked `FORCE ROW LEVEL SECURITY`. Every application statement touching a tenant-scoped table SHALL execute inside a transaction whose `app.current_tenant` GUC has been set to a validated UUID.

#### Scenario: Application role cannot bypass RLS
- **WHEN** the worker or API opens a connection to PgBouncer
- **THEN** the connection is authenticated as `zovark_app`, `SELECT rolbypassrls FROM pg_roles WHERE rolname = 'zovark_app'` returns `false`, and `SELECT relforcerowsecurity FROM pg_class WHERE relname = 'agent_tasks'` returns `true`

#### Scenario: Cross-tenant read is refused
- **WHEN** a handler running under `tenant_id = A` issues `SELECT * FROM investigations WHERE id = <uuid-owned-by-tenant-B>`
- **THEN** the query returns zero rows even without a `tenant_id` predicate in the WHERE clause

#### Scenario: SET LOCAL app.current_tenant rejects non-UUID input
- **WHEN** `beginTenantTx` (Go) or `_set_tenant(tenant_id)` (Python) is invoked with a tenant_id that is not a valid UUID
- **THEN** the helper returns an error before issuing any SQL and the transaction is not opened

### Requirement: Tenant-scoped Redis keys
Every Redis namespace used by the ingest, batch, dedup, circuit-breaker, and code-cache subsystems SHALL include the tenant_id as a mandatory key prefix. No Redis helper SHALL accept a call that omits tenant_id.

#### Scenario: Code cache cannot serve a cross-tenant entry
- **WHEN** tenant A stores an investigation plan under `task_type = brute_force, rule_name = SSH_BF` and tenant B submits an alert with the same `task_type` and `rule_name`
- **THEN** tenant B's lookup does not hit tenant A's cached entry and tenant B generates (or loads) its own plan

#### Scenario: Smart batcher does not mix alerts across tenants
- **WHEN** two tenants both submit alerts with `task_type = brute_force` and `source_ip = 185.220.101.45`
- **THEN** each tenant sees its own batch representative and the aggregated `raw_log` emitted downstream contains only alerts from a single tenant

### Requirement: SSE authentication via one-shot ticket
The dashboard SSE stream SHALL NOT accept the JWT access token in a query string. Instead the API SHALL issue short-lived single-use tickets, and the stream endpoint SHALL verify the ticket and burn it on first connection.

#### Scenario: SSE stream rejects a JWT-in-query request
- **WHEN** a client connects to `/api/v1/tasks/stream?token=<JWT>`
- **THEN** the API returns `401 ticket_required` and does not upgrade the connection

#### Scenario: Ticket is burned after first use
- **WHEN** a client obtains a ticket via `POST /api/v1/auth/sse-ticket`, successfully connects once, and then reconnects with the same ticket
- **THEN** the second connection receives `401 ticket_expired`

#### Scenario: Ticket expires after 30 seconds
- **WHEN** a client obtains a ticket and does not connect within the TTL
- **THEN** any subsequent connection attempt with that ticket receives `401 ticket_expired`

### Requirement: Constant-time comparison on all secret material
Every comparison of a user-supplied secret (webhook HMAC, platform-ingest Bearer token, SSE ticket, API key, TOTP) against a stored value SHALL use a constant-time comparator (`hmac.Equal` or `subtle.ConstantTimeCompare` in Go, `hmac.compare_digest` in Python).

#### Scenario: SIEM webhook HMAC uses hmac.Equal
- **WHEN** a webhook request arrives with a signature header
- **THEN** the handler computes the expected HMAC and compares it via `hmac.Equal`, never via `==`

#### Scenario: Platform-ingest Bearer uses ConstantTimeCompare
- **WHEN** a request arrives with an `Authorization: Bearer <token>` header on the platform ingest endpoint
- **THEN** the handler verifies the token with `subtle.ConstantTimeCompare` against the configured secret

### Requirement: OIDC fails closed on JWKS unavailability
The OIDC login path SHALL reject any ID token that has not been verified against a current JWKS. If JWKS cannot be fetched at startup the server SHALL refuse to register OIDC routes. If JWKS becomes unreachable at runtime the server SHALL serve login requests from a cached JWKS for up to `ZOVARK_OIDC_JWKS_STALE_AFTER` and then return `503 oidc_degraded`.

#### Scenario: Unsigned ID token is rejected
- **WHEN** the OIDC callback receives an ID token whose signature cannot be verified against any key in the cached JWKS
- **THEN** the handler returns `401 invalid_id_token` and no session is established

#### Scenario: Missing JWKS at startup
- **WHEN** the server starts with `OIDC_PROVIDER_URL` set and the JWKS fetch fails
- **THEN** the server logs a fatal error, does not register the OIDC callback routes, and fails its readiness probe

#### Scenario: Weak RSA key is rejected
- **WHEN** the JWKS fetch returns an RSA key whose modulus is fewer than 2048 bits
- **THEN** the key is discarded and a warning is logged; if no 2048-bit key remains, OIDC is treated as unavailable

### Requirement: OIDC JIT provisioning is scoped by issuer + external_auth_id
The OIDC just-in-time user provisioning flow SHALL match existing users by the composite `(iss, external_auth_id)` and SHALL NOT match by bare email. A new user SHALL only be provisioned if no existing user matches that composite in any tenant.

#### Scenario: Same email across tenants does not collide
- **WHEN** a user `alice@example.com` exists in tenant A and an OIDC login arrives from a different IdP for `alice@example.com` with a different `iss`+`sub`
- **THEN** the handler does not log in as tenant A's Alice; it either creates a new user in the login's configured tenant or returns `403 no_matching_user` per policy

### Requirement: Health endpoint reflects dependency state
`GET /health` SHALL return HTTP 503 when any critical dependency (Postgres, Redis/Valkey, Temporal) is unreachable. `GET /live` SHALL return HTTP 200 whenever the process is accepting connections. `GET /ready` SHALL return HTTP 200 iff the process finished startup and dependencies are reachable.

#### Scenario: Postgres outage returns 503 on /health
- **WHEN** the DB connection pool returns an error and `GET /health` is called
- **THEN** the handler returns HTTP 503 with a body listing `postgres: false`

#### Scenario: /live remains 200 during dependency outage
- **WHEN** the DB connection pool returns an error and `GET /live` is called
- **THEN** the handler returns HTTP 200

### Requirement: Pre-Temporal alert funnel fails closed
The three pre-Temporal layers (dedup, batch buffer, backpressure) SHALL fail closed on infrastructure errors. An error contacting Redis SHALL NOT cause the system to skip the check and admit more traffic.

#### Scenario: Backpressure fails closed on Redis outage
- **WHEN** Redis is unreachable during a backpressure check
- **THEN** the handler treats the request as if the soft limit were exceeded and either queues the task or returns `503 backpressure_unavailable`

#### Scenario: Drain goroutine is idempotent across replicas
- **WHEN** two API replicas both run the backpressure drain goroutine and contend for the same queued task
- **THEN** the underlying `SELECT` uses `FOR UPDATE SKIP LOCKED` inside a transaction and only one replica marks the task as `pending`

### Requirement: SIEM push-back is URL-safe and per-tenant
The SIEM verdict push-back SHALL validate the destination URL against a deny-list of private, link-local, loopback, and cloud-metadata addresses. Push-back configuration SHALL be per-tenant, not global. `InsecureSkipVerify` SHALL be rejected in production.

#### Scenario: SSRF to cloud metadata is refused
- **WHEN** `siem.pushback.url` is configured as `http://169.254.169.254/latest/meta-data/` and a verdict triggers push-back
- **THEN** the push-back handler refuses to send the request and logs a policy violation

#### Scenario: Tenant A cannot receive tenant B's verdicts
- **WHEN** tenant A configures push-back with URL X and tenant B configures push-back with URL Y
- **THEN** a verdict generated for tenant B is only sent to URL Y

### Requirement: Per-tool timeout is enforced
The v3 tool runner SHALL enforce `per_tool_timeout` using a real interruption mechanism (`ThreadPoolExecutor.future.result(timeout=...)`). A tool with an infinite loop SHALL be aborted at the configured timeout and SHALL NOT block its activity worker beyond the timeout + cleanup grace period.

#### Scenario: Infinite-loop tool is aborted
- **WHEN** an investigation plan includes a tool whose function enters `while True: pass`
- **THEN** the step is marked as `timeout` and the investigation proceeds to the next step within `per_tool_timeout + 1s`

### Requirement: Circuit breaker state is shared across workers
The LLM circuit breaker SHALL persist state in Redis with atomic update semantics. Two worker processes SHALL NOT diverge on the breaker state, and a single process SHALL NOT race on concurrent `update_state` calls.

#### Scenario: Breaker opens once for the whole fleet
- **WHEN** the LLM becomes unavailable and the failure threshold is crossed by one worker
- **THEN** all 32 worker processes observe `state = RED` on the next read without re-tripping the threshold independently

### Requirement: IOC provenance uses word-boundary matching
The assess-stage IOC provenance validator SHALL verify that every IOC appears in the raw log as a word-boundary match, not as a substring. `192.168.1.10` SHALL NOT be "confirmed" by a raw log that only contains `192.168.1.100`.

#### Scenario: Phantom IP is downgraded
- **WHEN** assess.py validates IOC `192.168.1.10` against a raw log that only contains `192.168.1.100`
- **THEN** the IOC's confidence is set to `low` and a provenance-mismatch log entry is emitted

### Requirement: NFKC normalisation does not corrupt stored evidence
The input sanitizer SHALL apply NFKC normalisation only to strings being checked against injection patterns. The raw log stored in `agent_tasks` and shown to analysts SHALL be the original (pre-normalisation) text.

#### Scenario: Half-width Japanese characters survive
- **WHEN** a SIEM alert arrives with `raw_log` containing `①` or `㎡`
- **THEN** the stored `raw_log` retains the original characters while the pattern scan still treats `①` as `1`

### Requirement: Test-harness patterns are not in the production sanitizer
The production `INJECTION_PATTERNS` list SHALL NOT contain test-fixture patterns (`tenant-uuid-\d+`, `other-tenant-uuid-\d+`, or similar). Production logs SHALL NOT be altered based on test fixture markers.

#### Scenario: Production raw log containing the phrase tenant-uuid-123 is unchanged
- **WHEN** a raw log containing the literal string `tenant-uuid-123` passes through the sanitizer
- **THEN** the sanitizer does not strip or flag the phrase

### Requirement: NOTIFY payloads respect the 8000-byte Postgres limit
Every `NOTIFY` emission (task_completed, investigation_events) SHALL measure payload size in UTF-8 bytes, not Python `len()`, and SHALL truncate or drop events whose payload would exceed 7900 bytes. Events SHALL be emitted over a pooled connection, not a per-event `psycopg2.connect`.

#### Scenario: Multibyte payload below Python len but above byte limit is truncated
- **WHEN** an event payload is 7000 Python characters but 28000 UTF-8 bytes
- **THEN** the event is truncated to fit under 7900 bytes before `NOTIFY` is issued

#### Scenario: Event emission reuses a pooled connection
- **WHEN** the pipeline emits 20 events across one investigation
- **THEN** no more than one Postgres connection is opened for event emission during that investigation

### Requirement: Secrets are not embedded in Docker image layers
The repository SHALL include a root `.dockerignore` that excludes `.env*`, `.git`, `models/`, `*.gguf`, `data/`, `dpo/`, `AUDIT_FINDINGS.md`, and `archive/` from the build context. Any credential present in a prior image layer SHALL be rotated.

#### Scenario: Build context does not contain .env
- **WHEN** `docker build` runs with the repo root as context
- **THEN** `.env` is not present in the sent tarball (verifiable via `docker build --progress=plain`)

### Requirement: Host ports are bound to loopback by default
In `docker-compose.yml`, host port mappings SHALL default to `127.0.0.1:` for every service that is not intended for public consumption. The public surface SHALL be limited to the ingress terminated at Caddy or nginx-proxy.

#### Scenario: Elasticsearch is not reachable from the host LAN
- **WHEN** the siem-lab profile is running and a laptop on the same LAN attempts `curl http://<host-ip>:9200/`
- **THEN** the connection is refused

### Requirement: The healer container does not directly mount docker.sock
The `healer` container SHALL route Docker API access through the `docker-socket-proxy` container. The direct bind `/var/run/docker.sock:/var/run/docker.sock` SHALL be removed.

#### Scenario: Healer has no docker.sock bind
- **WHEN** `docker inspect zovark-healer` is run and its `Mounts` list is inspected
- **THEN** no entry points at `/var/run/docker.sock`

### Requirement: Docker-socket-proxy runs without privileged mode
The `docker-socket-proxy` container SHALL NOT run with `privileged: true`. It SHALL drop all Linux capabilities and run with `no-new-privileges` and `read_only: true`.

#### Scenario: Proxy container capabilities
- **WHEN** `docker inspect zovark-docker-proxy` is run
- **THEN** `HostConfig.Privileged` is `false`, `HostConfig.CapDrop` contains `ALL`, and `HostConfig.SecurityOpt` contains `no-new-privileges:true`

### Requirement: init.sql is not the source of truth
The repository SHALL NOT contain `init.sql` as a parallel schema source. All schema SHALL be produced by `migrations/` and a squash migration SHALL bootstrap a fresh database.

#### Scenario: Fresh compose boot uses only migrations
- **WHEN** a developer runs `docker compose up postgres` on an empty volume
- **THEN** the database is populated by the migrator script (`scripts/apply_migrations.sh`) and no `init.sql` is referenced

#### Scenario: Migration ledger is consistent
- **WHEN** the migrator runs against a partially-migrated database
- **THEN** it skips every migration whose `version` already appears in `schema_migrations` and wraps each new migration in `BEGIN/COMMIT`

### Requirement: Monthly partitions exist for at least the next 12 months
The `investigations` and `audit_events` partitioned tables SHALL always have monthly partitions pre-created for at least the next 12 calendar months relative to the current date. A scheduled job SHALL create next-month partitions before the calendar crosses a month boundary.

#### Scenario: Partition maintenance runs before month boundary
- **WHEN** the current date is 2026-12-20 and the maintenance job runs
- **THEN** partitions for 2027-01 through 2027-12 exist

### Requirement: CI does not mask test failures
The GitHub Actions CI workflow SHALL run the integration test suite without `|| true` or any other failure-swallowing construct. Any test failure SHALL cause the job to exit non-zero.

#### Scenario: Failing integration test fails the CI job
- **WHEN** a PR introduces a change that breaks `tests/integration/test_e2e_pipeline.py` and CI runs
- **THEN** the integration-suite step exits non-zero and the job is marked failed

### Requirement: Smoke test script fails the shell on any alert failure
`scripts/smoke_test_100.sh` SHALL use `set -euo pipefail`, `curl -fsS` on every call, `jq` for JSON extraction, and SHALL exit non-zero if any alert fails to reach the expected verdict.

#### Scenario: One missed attack fails the script
- **WHEN** the smoke test submits 70 attacks and 1 attack is verdict'd as `benign`
- **THEN** the script prints the failing alert id and exits with status 1

### Requirement: Dashboard state-changing calls carry an Idempotency-Key
Every non-idempotent API call from the dashboard client (`dashboard/src/api/client.ts`) SHALL include an `Idempotency-Key` header generated with `crypto.randomUUID()` per user action. React StrictMode double-invokes and double-clicks SHALL NOT create duplicate tasks.

#### Scenario: Double-click creates one task
- **WHEN** a user double-clicks the "Run Investigation" button within 200 ms
- **THEN** only one task is created, verified by `SELECT count(*) FROM agent_tasks WHERE idempotency_key = <same uuid>` returning 1

### Requirement: Dashboard nginx serves security headers
The dashboard `nginx.conf` SHALL emit `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and `Strict-Transport-Security` (when TLS terminates at nginx). A `/health` endpoint returning `200 ok` SHALL be exposed for liveness probes.

#### Scenario: Security headers are set on every response
- **WHEN** a client fetches `/index.html` from the dashboard container
- **THEN** the response contains all five listed headers with the specified values

### Requirement: Unit tests cover every pipeline stage
`worker/tests/` SHALL contain a `test_<stage>.py` file for each of `ingest`, `analyze`, `execute`, `assess`, `govern`, `store`. Each file SHALL exercise at least one happy path and one error path. Integration tests SHALL NOT be the only coverage for these stages.

#### Scenario: test_analyze.py exists and runs under pytest
- **WHEN** a developer runs `pytest worker/tests/test_analyze.py`
- **THEN** at least one test exercises the saved-plan fast path and at least one exercises the LLM-tool-selection fallback path

### Requirement: RLS is covered by multi-tenant tests
`worker/tests/` SHALL contain a `test_rls.py` that provisions two tenants and asserts that tenant A cannot read tenant B's `investigations`, `agent_tasks`, or `audit_events` rows when running under the `zovark_app` role.

#### Scenario: Cross-tenant SELECT returns zero rows
- **WHEN** the test opens a transaction under `tenant_id = A` and runs `SELECT * FROM investigations WHERE id = <B's uuid>`
- **THEN** the query returns zero rows

### Requirement: Circuit breaker, pushback, backpressure, governance, and dedup are unit-tested
The test suite SHALL include tests that exercise: the circuit-breaker open/half-open/closed transitions; the `siem_pushback` retry backoff; the backpressure hard-limit 503 response; governance autonomy-slider transitions (manual → semi → auto); and the dedup race (severity escalation + TTL expiry) against `fakeredis` (not `MagicMock`).

#### Scenario: Circuit breaker state-machine test
- **WHEN** the test records `cb_failures` equal to the RED threshold on a fresh breaker
- **THEN** `cb.state` transitions from `GREEN` to `RED` and subsequent `cb.allow()` calls return `False` until the recovery threshold is crossed

### Requirement: AutoResearch red-team evaluator drives the real pipeline
`autoresearch/redteam/evaluate.py` SHALL drive the actual investigation pipeline (analyze → execute → assess) and compare the resulting `verdict` / `risk_score` against a ground-truth label. It SHALL NOT score bypasses via static regex on `sanitize_siem_event`. The payload set SHALL be split into `train.jsonl` / `holdout.jsonl` and fitness SHALL be evaluated only on holdout.

#### Scenario: A payload that sanitize_siem_event strips but analyze still catches is not a bypass
- **WHEN** the evaluator runs a payload whose sanitizer output preserves an attack indicator but whose end-to-end verdict is `true_positive`
- **THEN** the payload is scored 0 (not a bypass), regardless of sanitizer output
