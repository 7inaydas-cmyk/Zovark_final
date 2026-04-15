## Why

The user reported that `scripts/e2e_probe.sh` stalls at Stage 1 (ingest) on a freshly-up stack: login (Stage 0) passes, but `POST /api/v1/tasks` returns **HTTP 500 `{"error":"an internal error occurred"}`** with no Redpanda topics ever being created (`rpk topic list` shows zero topics, `docker logs zovark-api` has no kafka/publish/error lines).

A diagnose-only audit ([Phase 1, this branch](../../../scripts/e2e_probe.sh)) confirmed the request never reaches the publish path. The `X-Zovark-Trace-Id` from the failed curl maps in the API logs to:

```
{"level":"INFO","msg":"[ERROR] create task record: FATAL: prepared statement name is already in use (SQLSTATE 08P01)"}
POST /api/v1/tasks → 500 in 16.7ms
```

### Root cause: pgx named prepared statements collide on PgBouncer transaction-pooled backends

The Go API connects to Postgres through PgBouncer in **transaction pooling mode**:

```
DATABASE_URL=postgresql://zovark:hydra_dev_2026@pgbouncer:5432/zovark
```

`pgx` (v5) defaults to `QueryExecModeCacheStatement`, which automatically prepares every parameterised query as a **named prepared statement** on whatever backend connection it currently holds. In transaction pooling, PgBouncer multiplexes a small pool of real Postgres backends across many client connections, **per-transaction**. So the backend handed to the API for a `INSERT INTO agent_tasks ... RETURNING id` may already hold a prepared statement with the same auto-generated name from a previous client's transaction. PostgreSQL rejects re-prepare with **`SQLSTATE 08P01` "prepared statement name is already in use"** (FATAL — kills the connection).

This explains every observed symptom:

1. `createTaskHandler()` (`api/task_handlers.go:214`) calls `dbPool.QueryRow(...)` to insert the new task row.
2. pgx tries to (re-)register a named prepared statement on a recycled PgBouncer backend → **08P01 FATAL**.
3. `respondInternalError(c, err, "create task record")` returns HTTP 500 with the sanitised body the user saw.
4. Execution **never reaches** `publishTaskNew(...)` at `api/task_handlers.go:263`. That is why `rpk topic list` is empty, why no `tasks.new.*` topic ever auto-creates, and why the API logs contain zero redpanda/kafka lines for the failed request.
5. Login (`POST /api/v1/auth/login`) and `/ready` happen to work because their query patterns avoid the collision on the specific backend they get handed — pure luck of statement-cache state. Every task insert reproduces.

This is a known incompatibility documented by both pgx and PgBouncer projects. The fix is to tell pgx to either skip named prepared statements entirely (simple protocol) or to use unnamed prepared statements (describe mode), both of which are safe under PgBouncer transaction pooling.

### Why the previous `fix-e2e-ingest-stall` change did not catch this

That change targeted Stage 3 (consumer pattern subscription) and validated end-to-end on a stack where the API's pgx pool happened to have a warm statement cache that didn't collide. The Stage 1 failure mode only surfaces reliably after a fresh `docker compose up -d` against a cold pgx pool with PgBouncer multiplexing. The e2e probe has no Stage 0.5 sanity check that proves the API can write a row before the publish attempt.

## What Changes

- **Set `default_query_exec_mode=describe_exec` on the API's pgx pool** so pgx stops issuing named prepared statements that survive across transactions but still does a `Describe` round-trip per query so it can infer parameter OIDs for `map[string]interface{}` → `jsonb` values (which the API uses heavily for `agent_tasks.input` / `output`). The natural place is `api/db.go` where `pgxpool.ParseConfig` builds the pool config — set `cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeDescribeExec` once for the whole pool.
- **Honour an env override**: `ZOVARK_PGX_QUERY_MODE` (default `describe_exec`) accepts `describe_exec`, `exec`, `simple_protocol`, and `cache_statement`. Operators can flip to `exec` (faster, no Describe) when no parameter relies on OID inference, or to `cache_statement` when bypassing PgBouncer entirely (e.g., direct postgres connection in tests). **Note**: an earlier draft of this change picked plain `exec` as the default. Live testing showed `Exec` mode cannot encode untyped `map[string]interface{}` parameters into `jsonb` columns (`unable to encode … OID 0: cannot find encode plan`), because it skips the Describe round-trip pgx normally uses to learn the target column type. `describe_exec` is the correct default — it is still PgBouncer-safe (no named prepared statements, no per-conn cache) but issues the `Describe` so jsonb maps just work.
- **Add a startup self-test in `api/db.go` `initDB()`**: after the pool is built, run a parameterised `INSERT INTO ... RETURNING` against a temp table (or a benign `SELECT $1::int` that exercises the same code path twice on different backend connections), and fail-fast with a clear log line if pgx falls back to a mode that triggers `08P01`. Catches regressions where someone unsets the env or changes the pool config.
- **Add a Stage 0.5 "db_write" sanity probe to `scripts/e2e_probe.sh`** that POSTs a no-op `INSERT` via the existing health/diagnostic endpoint (or a new admin probe) and asserts a 2xx before Stage 1 even tries. If pgx ever silently regresses to named prepared statements again, the probe names the failure as a DB write fault, not "ingest stall".
- **Add a runbook section to `docs/RUNBOOK_HEALTHCHECK.md`** ("API returns 500 on POST /api/v1/tasks with empty Redpanda topic list") describing the symptom, the `08P01` log line, and the env override.

## Capabilities

### New Capabilities

- (none — this change hardens existing API ↔ Postgres wiring and extends the existing `e2e-pipeline-probe` capability)

### Modified Capabilities

- `e2e-pipeline-probe`: gains a Stage 0.5 `db_write` sanity probe so a DB-write failure is reported as such instead of being misdiagnosed as an ingest stall. Stage 1 only runs after Stage 0.5 passes.

## Impact

- **Affected code**: `api/db.go` (pgx pool config + startup self-test), `api/main.go` (env wiring if `ZOVARK_PGX_QUERY_MODE` is read at boot), `scripts/e2e_probe.sh` (new Stage 0.5), `docs/RUNBOOK_HEALTHCHECK.md` (new section).
- **Runtime impact**: pgx switches from `QueryExecModeCacheStatement` to `QueryExecModeExec`. Every query is sent as a one-shot `Query` message instead of `Parse`+`Bind`+`Execute`. For the Zovark API workload (mostly small INSERT/SELECT in handlers, no high-frequency hot loops) the latency delta is sub-millisecond per query and dominated by network + PgBouncer. No measurable throughput impact at expected QPS.
- **Risk**: low. `QueryExecModeExec` is the **documented pgx mode for PgBouncer transaction pooling** (see pgx README "PgBouncer" section). It removes a known footgun and adds startup self-test to prevent silent regression.
- **Breaking**: none. The change is internal to the API's DB driver config; SQL, schemas, and external interfaces are unchanged. The new env var has a safe default.
