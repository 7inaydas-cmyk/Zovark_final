## ADDED Requirements

### Requirement: API pgx pool defaults to PgBouncer-safe query exec mode
The Go API's pgx connection pool (`api/db.go` `initDB`) SHALL default to a query exec mode that is safe under PgBouncer transaction pooling — specifically, a mode that (a) does NOT issue named prepared statements that survive across transactions, AND (b) DOES issue a per-query `Describe` round-trip so pgx can infer parameter OIDs for values whose Go type doesn't pin down a Postgres type (notably `map[string]interface{}` → `jsonb`). The default SHALL be `pgx.QueryExecModeDescribeExec`. The mode SHALL be overridable via the `ZOVARK_PGX_QUERY_MODE` environment variable, with accepted values `describe_exec` (default), `exec`, `simple_protocol`, and `cache_statement`. Any value other than these four SHALL cause `initDB` to return an error before the pool is built.

#### Scenario: Default mode against a PgBouncer-fronted DATABASE_URL
- **WHEN** the API is started with `DATABASE_URL=postgresql://zovark:…@pgbouncer:5432/zovark` and `ZOVARK_PGX_QUERY_MODE` unset
- **THEN** the pgx pool is built with `cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeDescribeExec` and the API logs the line `pgx_pool_query_mode mode=describe_exec` at startup

#### Scenario: Override to exec
- **WHEN** the API is started with `ZOVARK_PGX_QUERY_MODE=exec`
- **THEN** the pgx pool is built with `cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeExec` and the API logs `pgx_pool_query_mode mode=exec`. (Note: `exec` mode cannot encode `map[string]interface{}` into `jsonb` columns; operators choosing this override accept that all jsonb parameters at every callsite use explicit `$N::jsonb` casts or pre-marshalled `[]byte`.)

#### Scenario: Invalid override value
- **WHEN** the API is started with `ZOVARK_PGX_QUERY_MODE=foobar`
- **THEN** `initDB` returns an error containing the substring `unknown ZOVARK_PGX_QUERY_MODE value` and the API process exits non-zero before binding the listener

### Requirement: API performs a startup pgx self-test that catches `08P01` regressions
After `dbPool.Ping()` succeeds in `initDB`, the API SHALL run a self-test that issues two parameterised round-trips on different backend connections (forced by `acquire-release-acquire` against the pool) and SHALL return an error from `initDB` if either round-trip fails with PostgreSQL SQLSTATE `08P01` (`prepared statement … already exists`). The error message SHALL name the env var `ZOVARK_PGX_QUERY_MODE`, the safe value `describe_exec` (and the alternate `exec`), and SHALL include a pointer to the runbook section `docs/RUNBOOK_HEALTHCHECK.md#api-08p01`.

#### Scenario: Self-test passes under describe_exec mode (default)
- **WHEN** the API starts with `ZOVARK_PGX_QUERY_MODE` unset (or `=describe_exec`) against a PgBouncer-fronted DATABASE_URL
- **THEN** the self-test issues two `SELECT $1::int` round-trips on distinct backend connections, both return successfully, and the API logs `pgx_pool_self_test result=passed` before binding the listener

#### Scenario: Self-test fails under cache_statement mode
- **WHEN** the API starts with `ZOVARK_PGX_QUERY_MODE=cache_statement` against a PgBouncer-fronted DATABASE_URL
- **THEN** the self-test triggers a `08P01` error within five seconds, `initDB` returns an error containing the substrings `ZOVARK_PGX_QUERY_MODE`, `describe_exec`, and `RUNBOOK_HEALTHCHECK.md`, and the API process exits non-zero without binding the listener

#### Scenario: Self-test passes under direct postgres connection
- **WHEN** the API starts with `DATABASE_URL=postgresql://…@postgres:5432/zovark` (bypassing PgBouncer) and `ZOVARK_PGX_QUERY_MODE=cache_statement`
- **THEN** the self-test issues both round-trips on the same backend (no multiplexing), both succeed, and the API logs `pgx_pool_self_test result=passed`

### Requirement: Admin DB-write probe endpoint
The API SHALL expose an admin-only endpoint `POST /api/v1/admin/diagnostics/probe-db` that performs an `INSERT … RETURNING id` followed by a `DELETE` against a dedicated `probe_writes(id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now())` table inside a single transaction, and returns `{"ok": true, "took_ms": <int>, "row_id": "<uuid>"}` on success or HTTP 500 with `{"error": "an internal error occurred", "trace_id": "<uuid>"}` on failure. The endpoint SHALL be gated behind the existing `requireRole("admin")` middleware. The probe SHALL NOT accept any body parameters and SHALL NOT write to any table other than `probe_writes`.

#### Scenario: Admin caller, healthy DB
- **WHEN** an admin-token holder POSTs to `/api/v1/admin/diagnostics/probe-db` against a healthy stack
- **THEN** the response is HTTP 200 with `ok: true`, `took_ms` is a non-negative integer, and `row_id` is a parseable UUID. After the request returns, `SELECT count(*) FROM probe_writes` is zero (the row was deleted in the same transaction).

#### Scenario: Non-admin caller
- **WHEN** an analyst-token holder POSTs to `/api/v1/admin/diagnostics/probe-db`
- **THEN** the response is HTTP 403 and no row is inserted into `probe_writes`

#### Scenario: DB write fails with 08P01
- **WHEN** an admin caller POSTs to the endpoint and the underlying pgx round-trip returns SQLSTATE `08P01`
- **THEN** the response is HTTP 500 with the sanitised error body, the `X-Zovark-Trace-Id` header is set, and the API logs a line containing `[ERROR] probe-db` followed by the underlying SQLSTATE

### Requirement: Operator runbook documents the `08P01` symptom
`docs/RUNBOOK_HEALTHCHECK.md` SHALL contain a section anchored as `#api-08p01` titled "API returns 500 on POST /api/v1/tasks (08P01 prepared statement collision)" that names the symptom (HTTP 500, empty `rpk topic list`, `08P01` log line), the root cause (pgx default mode incompatible with PgBouncer transaction pooling), and the fix (set `ZOVARK_PGX_QUERY_MODE=exec`). The section SHALL also state that this knob MUST NOT be set to `cache_statement` when `DATABASE_URL` points at PgBouncer.

#### Scenario: Operator follows the link from the startup error
- **WHEN** an operator sees the API startup `FATAL` line referencing `RUNBOOK_HEALTHCHECK.md#api-08p01`
- **THEN** opening the runbook at that anchor lands on the correctly-titled section, and the section names both the env var and the safe value

## MODIFIED Requirements

### Requirement: e2e probe runs sequential stages with named pass/fail records
`scripts/e2e_probe.sh` SHALL execute its stages in a fixed sequence and SHALL record exactly one `pass`, `fail`, or `skip` outcome per stage to the timeline. The stage sequence SHALL be: `0 login`, `0.5 db_write`, `1 ingest`, `2 redpanda`, `3 pg.investigating`, `4 pg.completed`, `5 signoz`, `6 verdict`, `7 cleanup`. A failing stage SHALL abort all subsequent stages (they SHALL be recorded as `skip` with detail `prerequisite failed: <stage name>`).

#### Scenario: All stages pass on a healthy stack
- **WHEN** the operator runs `scripts/e2e_probe.sh` against a healthy stack and all nine stages succeed
- **THEN** the timeline contains exactly nine rows in the order `0 login`, `0.5 db_write`, `1 ingest`, `2 redpanda`, `3 pg.investigating`, `4 pg.completed`, `5 signoz`, `6 verdict`, `7 cleanup`, each marked `pass`, and the script exits 0

#### Scenario: db_write stage fails — ingest skipped
- **WHEN** the API's pgx pool is misconfigured and `POST /api/v1/admin/diagnostics/probe-db` returns 500
- **THEN** Stage 0.5 records `fail` with detail starting with `http=500` and containing the substring `08P01` if that SQLSTATE is in the response body, AND Stages 1-7 each record `skip` with detail `prerequisite failed: 0.5 db_write`, AND the script exits non-zero

#### Scenario: Login stage fails — db_write skipped
- **WHEN** the API's `/api/v1/auth/login` returns 401
- **THEN** Stage 0 records `fail`, Stages 0.5-7 each record `skip` with detail `prerequisite failed: 0 login`, and the script exits non-zero

### Requirement: db_write stage POSTs to the admin probe endpoint and asserts a 2xx
`scripts/e2e_probe.sh` Stage 0.5 SHALL `curl -X POST` the admin DB-write probe endpoint with the JWT obtained from Stage 0 and SHALL parse the response body as JSON. It SHALL emit `pass` if and only if the HTTP status is 200 AND the response JSON has `ok: true` AND `row_id` parses as a UUID. On any other outcome it SHALL emit `fail` with a detail string of the form `http=<code> body=<first 120 chars>` and a one-line operator hint pointing at `docker logs zovark-api | grep 08P01` when the body or status suggests a prepared-statement issue.

#### Scenario: Successful probe
- **WHEN** the admin probe endpoint returns 200 with `{"ok": true, "took_ms": 4, "row_id": "..."}`
- **THEN** Stage 0.5 records `pass` with detail starting with `took_ms=` and the script proceeds to Stage 1

#### Scenario: 08P01 detected in error body
- **WHEN** the admin probe endpoint returns 500 and the response body or correlated API log line contains the substring `08P01`
- **THEN** Stage 0.5 records `fail` with detail containing `08P01` and the script prints a one-line hint of the form `hint: pgx pool may be in cache_statement mode against PgBouncer — set ZOVARK_PGX_QUERY_MODE=exec; see docs/RUNBOOK_HEALTHCHECK.md#api-08p01`
