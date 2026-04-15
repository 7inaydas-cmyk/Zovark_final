## Context

The Zovark Go API connects to PostgreSQL through PgBouncer in **transaction pooling mode** (`docker-compose.yml` → `pgbouncer:5432` → `postgres:5432`, `POOL_MODE=transaction`, 400 client / 25 server). Transaction pooling is the right choice for this workload (bursty short transactions across many request handlers, small backend pool, RLS via `SET LOCAL` per transaction). The codebase already accommodates the constraints PgBouncer imposes — see `api/db.go:90-93` where `beginTenantTx()` interpolates `tenant_id` into `SET LOCAL app.current_tenant = '...'` instead of using `$1` parameter binding, with a comment explaining why.

What was *not* accommodated is the second well-known PgBouncer transaction-pooling constraint: **named prepared statements cannot be reused across server connections**. `pgx/v5` defaults to `QueryExecModeCacheStatement`, which silently issues `Parse name=stmt_xxxx` followed by `Bind/Execute` for every parameterised query. Under transaction pooling the same logical SQL gets prepared with the same auto-generated name on whichever backend connection happens to be assigned to the current transaction, and PgBouncer then hands that backend to a different client whose pgx instance also tries to `Parse stmt_xxxx`. Postgres responds **`08P01 prepared statement "stmt_xxxx" already exists`** — a `FATAL` error, killing the connection and returning an error to the caller.

Empirically (Phase 1 audit on this branch, 2026-04-13), this manifests as `POST /api/v1/tasks` returning HTTP 500 with the API log line `[ERROR] create task record: FATAL: prepared statement name is already in use (SQLSTATE 08P01)` while `GET /ready` and `POST /api/v1/auth/login` continue to succeed. The handler returns the error from `respondInternalError(c, err, "create task record")` at `api/task_handlers.go:214`, so execution **never reaches** `publishTaskNew(...)` at line 263 — which is why `rpk topic list` is empty and the API logs contain zero redpanda/kafka entries for the failed request. Cycle 8's three pre-Temporal funnel layers (dedup, batch, backpressure) all sit *after* the failing `INSERT` and so are also bypassed.

## Goals / Non-Goals

**Goals:**

- Make `POST /api/v1/tasks` succeed against the existing PgBouncer transaction-pooling deployment, every time, on a cold pool.
- Encode the fix as the **API's pgx default**, not as per-callsite query-mode overrides — there are >50 `dbPool.QueryRow/Exec` callsites and any handler that forgets to set the mode reproduces the bug.
- Add a **startup self-test** that fails fast (with a clear log line naming the env var to override) if the pool is ever built in a mode that triggers `08P01`. Prevents silent regression after a future pgx upgrade or a refactor that "tidies up" the config.
- Add a **Stage 0.5 db_write probe** to `scripts/e2e_probe.sh` so a future regression in the same area is reported as "DB write failed" instead of "ingest stalled" (the existing failure mode wasted a debugging session).
- Document the failure mode and fix in `docs/RUNBOOK_HEALTHCHECK.md` so an operator who sees `08P01` in the API logs has a one-page link to the cause and the override.

**Non-Goals:**

- Switching PgBouncer to session pooling. Session pooling would also fix the issue but at the cost of pinning every Postgres backend to one client for its full lifetime, defeating the purpose of using PgBouncer at all for our 400-client / 25-server config. Out of scope.
- Bypassing PgBouncer (connecting the API directly to `postgres:5432`). Removes connection multiplexing entirely and breaks the documented production deployment topology. Out of scope.
- Reworking how `worker/` (Python, asyncpg) talks to Postgres. asyncpg has its own prepared-statement caching (`statement_cache_size=0` is the analogous knob) and a separate audit; this change is API-only.
- Changing the `dpo/` or `autoresearch/` Python tooling. They already use direct `postgres:5432` connections with `psycopg2` and are unaffected.
- Any database schema, RLS policy, or tenant-isolation change. The fix is purely in the pgx driver config.

## Decisions

### Decision 1: Use `pgx.QueryExecModeDescribeExec`, not `QueryExecModeExec`, `QueryExecModeSimpleProtocol`, or `QueryExecModeCacheStatement`

`pgx/v5` exposes five query exec modes for PgBouncer compatibility:

| Mode | How it sends queries | PgBouncer txn pool safe? | Encodes `map[string]interface{}` → `jsonb`? |
|------|---------------------|--------------------------|---------------------------------------------|
| `QueryExecModeCacheStatement` (pgx default) | `Parse(name)` + `Bind/Execute`, statement cached per-conn | **No** — collides on `08P01` | Yes (Describe-cached) |
| `QueryExecModeCacheDescribe` | `Parse(unnamed)` + `Describe` cached per-conn | **No** — Describe cache also collides under multiplexing | Yes (Describe-cached) |
| `QueryExecModeDescribeExec` | `Parse(unnamed)` + `Describe` per query, no cache | **Yes** | **Yes** — fresh Describe each time |
| `QueryExecModeExec` | `Parse(unnamed)` + `Bind` + `Execute`, no Describe | **Yes** | **No** — pgx has no OID for the parameter, errors with `OID 0: cannot find encode plan` |
| `QueryExecModeSimpleProtocol` | Plain text query, no extended protocol | **Yes** | No (text-encoded; complex types via custom Stringer only) |

Choice: **`QueryExecModeDescribeExec`**.

Why not `SimpleProtocol`: it sends params as plain text strings, so `pgx` has to format `time.Time`, `uuid.UUID`, `[]byte`, and JSON values as text and then Postgres re-parses them. We have multiple handlers that pass `pgtype.JSONB` and `time.Time` parameters to `INSERT`s; text encoding adds correctness risk for marginal benefit.

Why not plain `Exec`: a previous draft of this change picked `Exec` for the extra perf (no Describe round-trip per query). Live testing against the deployed stack revealed that the Zovark API passes `map[string]interface{}` directly as the `agent_tasks.input` `jsonb` parameter in `createTaskHandler` — and `Exec` mode reports `unable to encode map[string]interface {}{…} into text format for unknown type (OID 0): cannot find encode plan`. Without a Describe round-trip pgx has no way to learn that `$N` targets a `jsonb` column, and there is no static Go-type → Postgres-OID mapping for `map[string]interface{}` (it's a nominal Go type that could mean many Postgres types). Fixing this with `Exec` would require either (a) explicit `$N::jsonb` casts at every callsite that passes a map (>10 sites, easy to miss one and silently regress), or (b) marshalling every map to `[]byte` before the call (intrusive, breaks the existing handler shape). `DescribeExec` solves it for free.

Why `DescribeExec` is safe: it uses the extended query protocol but issues `Parse` with an **empty statement name** (the unnamed prepared statement, which Postgres allows to be re-prepared on every call), then `Describe` (which returns the column types for this round-trip only — nothing cached), then `Bind/Execute`. Nothing is cached on the backend connection, so PgBouncer multiplexing has nothing to collide on. The pgx README explicitly lists this mode (alongside `Exec`) as PgBouncer-transaction-mode-safe.

Cost of the `Describe` round-trip: one extra wire round-trip per query. For Zovark's workload (small per-handler queries dominated by network and PgBouncer hops, no tight inner loops in the API path) the latency delta is sub-millisecond and lost in the noise. Validated empirically: Stage 0.5 `db_write` reports `took_ms=1` consistently against the live stack (full INSERT+RETURNING+DELETE in one transaction).

Alternatives considered:
- **Plain `QueryExecModeExec` with explicit `$N::jsonb` casts at every map callsite**: rejected. Faster per-query (no Describe), but requires touching every handler that passes a map and silently breaks any future handler that forgets the cast. Default-everywhere correctness > marginal latency win.
- **Set the mode per-query at every callsite** (`dbPool.QueryRow(ctx, sql, pgx.QueryExecModeDescribeExec, args...)`): rejected — one missed callsite reproduces the original `08P01` bug, and there are >50 of them. Default-everywhere is the only safe shape.
- **Switch driver to `database/sql` + `lib/pq`**: rejected — would lose `pgx`'s OTEL tracer wrapping (`otelpgx` at `api/db.go:23-29`), pgtype JSON support, and tenant tx helpers. Massive blast radius for a config-knob bug.

### Decision 2: Wire the choice through one env var, default `describe_exec`

Add `ZOVARK_PGX_QUERY_MODE` (default `describe_exec`) read by `initDB()` in `api/db.go`. Accepted values: `describe_exec`, `exec`, `simple_protocol`, `cache_statement` (the dangerous mode, allowed only for direct-postgres test runs).

Why an env var at all: integration tests in `api/_test.go` files spin up a fresh Postgres container and connect directly (no PgBouncer). Forcing `describe_exec` in tests is harmless but limits coverage of pgx's per-conn statement cache path. The env var lets a test config opt back into `cache_statement` against a direct connection without code changes.

Why default `describe_exec` (not `cache_statement`, not `exec`): the production-shaped configuration is the deployed one (PgBouncer in front), and the dev compose stack also uses PgBouncer. Defaulting to the PgBouncer-safe + jsonb-friendly mode means a fresh `docker compose up -d` Just Works for every handler, including the ones that pass `map[string]interface{}` into `agent_tasks.input`. Tests that need the binary statement-cache path set `ZOVARK_PGX_QUERY_MODE=cache_statement` explicitly. Operators who want the per-query Describe round-trip removed (and are confident no callsite passes a map without an explicit cast) can set `ZOVARK_PGX_QUERY_MODE=exec`.

### Decision 3: Self-test on startup, not on every request

In `initDB()` after `dbPool.Ping()` succeeds, run a parameterised round-trip that *would* fail with `08P01` if the mode is wrong: e.g., acquire two distinct backend connections from the pool in sequence (`Acquire`→`Release`→`Acquire`) and run `SELECT $1::int` on each. On any `08P01` error, log a `FATAL` line of the form:

```
FATAL: pgx pool built with prepared-statement mode incompatible with PgBouncer transaction pooling.
       Set ZOVARK_PGX_QUERY_MODE=describe_exec (or exec, or use direct postgres without pgbouncer for tests).
       See docs/RUNBOOK_HEALTHCHECK.md#api-08p01.
```

…and return an error from `initDB()` so the API process exits before binding the listener. Crashes loud at startup, not silent on the first task ingest.

Why not test on every request: per-request self-tests double the DB load on the hot path for a config that only changes at process boot. Startup is the right place.

Why not check the env directly: a future refactor could rename the env or skip reading it. Self-testing the *behaviour* of the live pool is the only way to catch a silent regression.

### Decision 4: Stage 0.5 `db_write` probe in `scripts/e2e_probe.sh`

Add a new stage between login and ingest:

```
Stage 0.5 db_write — POST /api/v1/admin/diagnostics/probe-db
```

The endpoint runs an `INSERT` into a small probe table (`probe_writes(id uuid, created_at timestamptz)`), `RETURNING id`, then `DELETE` of that row in the same transaction. Returns `{"ok": true, "took_ms": …}` or 500 on failure.

Why a new endpoint instead of reusing `/ready`: `/ready` does a `SELECT 1` (no parameter binding), which doesn't exercise the `Parse/Bind/Execute` path — pgx sends a fast-path simple query for parameterless `SELECT 1` regardless of mode. The probe needs a parameterised `INSERT ... RETURNING id` to actually trigger the `08P01` reproducer.

Stage 0.5 runs after Stage 0 (login, requires JWT) and before Stage 1 (ingest). On failure it records `record_stage "0.5 db_write" "fail" ...` and aborts the probe with a clear "API cannot write to Postgres — check 'docker logs zovark-api | grep 08P01'" hint.

### Decision 5: No change to `worker/` Python config in this change

Symptom is API-only. The worker's PostgreSQL access (asyncpg via `psycopg` in `worker/stages/store.py`, plus the events.py NOTIFY emitter) is in a separate audit and any fix there would need its own probe and self-test. Keeping the change focused.

## Risks / Trade-offs

- **[Risk] `QueryExecModeExec` has slightly different type-handling semantics for some pgtype variants** → Mitigation: run the existing API integration test suite (`go test ./api/...`) and the smoke test (`scripts/smoke_test_100.sh`) before merge. The bulk of the API uses `dbPool.QueryRow/Exec` with primitive Go types (`string`, `int`, `time.Time`, `uuid.UUID`, `[]byte`, `pgtype.JSONB`), all of which `Exec` mode handles identically to `CacheStatement`. The known divergence is around custom enum types decoded as `pgtype.Text` vs `string`, which the Zovark schema does not use.
- **[Risk] Per-query latency overhead from skipping the prepared-statement cache** → Mitigation: `Exec` mode still uses the extended query protocol with binary encoding, so the only loss is the per-statement plan cache on the Postgres side. For Zovark's workload (small per-handler queries, no tight inner loops, dominated by network and PgBouncer hop) this is sub-millisecond per query and lost in the noise. Validated empirically by re-running `scripts/smoke_test_100.sh` after the change and comparing `agent_tasks.duration_ms`.
- **[Risk] Startup self-test could itself be flaky on a slow CI runner** → Mitigation: the self-test runs two trivial `SELECT $1::int` statements with a 5-second context timeout. If both succeed, mode is safe; if either errors with `08P01`, mode is broken. Slow CI runners simply take longer to pass, not fail.
- **[Risk] Stage 0.5 endpoint becomes a backdoor write API** → Mitigation: the probe table is dedicated (`probe_writes`), gated behind the existing admin RBAC middleware (`requireRole("admin")`), and the handler ONLY ever writes a transient row that it deletes in the same transaction. No tenant data, no user input, fixed SQL.
- **[Risk] An operator who copy-pastes the dangerous `cache_statement` value from the env-var docs into a PgBouncer-fronted prod pool** → Mitigation: the startup self-test catches it on the next API restart and exits with a `FATAL` line that names the safe value. The runbook section explicitly says "do not set this to `cache_statement` if your DATABASE_URL points at PgBouncer".

## Migration Plan

This change is a config-only behaviour change inside one Go binary. No DB schema migration, no SIEM-side change, no dashboard change.

1. Merge to `audit/execution-fixes` branch.
2. `docker compose build api && docker compose up -d api`.
3. Watch `docker logs zovark-api | head -20` for the new startup line `pgx_pool_query_mode=exec self_test=passed`.
4. Run `scripts/e2e_probe.sh` end-to-end. Stage 0.5 db_write must pass; Stage 1 ingest must pass; Stages 2-7 must pass.
5. Run `scripts/smoke_test_100.sh`. All 70 attacks must still detect; latency baseline must be within 10 % of the pre-change run.

**Rollback**: set `ZOVARK_PGX_QUERY_MODE=cache_statement` and rebuild the API. The startup self-test will fail and the API will refuse to start — at which point the operator knows the rollback is *itself* the bug. There is intentionally no path to silently roll this back; the only way to revert the new behaviour is to revert the commit.
