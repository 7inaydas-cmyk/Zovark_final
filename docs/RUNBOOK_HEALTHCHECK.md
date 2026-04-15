# Healthcheck Runbook

Operator reference for `scripts/stack_healthcheck.sh` and `scripts/e2e_probe.sh`.

This runbook complements the scripts' `--help` output with the context an
on-call engineer needs to act on a red row at 3 AM.

---

## scripts/e2e_probe.sh — end-to-end pipeline probe

Submits **one** synthetic alert and tracks it through every pipeline stage.
This is the only probe that answers "is a new alert flowing through the stack
right now?" because it actually submits one and watches where it lands.

### Stages

| # | Stage | What it checks |
|---|---|---|
| 0 | `setup` | Generate probe id, login, capture Signoz span-count baselines for `zovark-api` and `zovark-worker` |
| 1 | `ingest` | `POST /api/v1/tasks` with `task_type=probe_noop` + `input.synthetic=true` + `input.probe_id=<UUID>`. Asserts HTTP 200/201/202 and a non-empty `task_id` in the response. |
| 2 | `redpanda` | `rpk topic consume tasks.new.<tenant> --offset end --num 20` inside the `zovark-redpanda` container. Asserts the freshly-ingested `task_id` appears in the last 20 messages. Retries up to 3× with a 1-second backoff. |
| 3 | `pg.investigating` | Polls `agent_tasks.status` every 500 ms for up to 90 seconds. Records the first timestamp where status exits `pending` / `queued`. |
| 4 | `pg.completed` | Polls for a terminal state (`completed` / `needs_review` / `needs_analyst_review` / `failed` / `error`). |
| 5 | `signoz` | Re-queries `GET /api/v1/services` and asserts both `zovark-api` and `zovark-worker` span counts increased vs the Stage 0 baselines. |
| 6 | `verdict` | Reads `output->>'verdict'` and `output->>'risk_score'`. For `probe_noop` the expected result is `verdict=benign risk_score=5`. |
| 7 | `cleanup` | `UPDATE agent_tasks SET input = jsonb_set(input, '{_probe_cleanup}', 'true'::jsonb)`. Keeps the row (does **not** delete) so operators can inspect it later. |

### Reading the output

```
E2E Pipeline Probe — probe_id: 8f2a6e…
--------------------------------------------------------------------
  STAGE                  AT       +ms    HOP   STATUS    DETAIL
--------------------------------------------------------------------
  0 setup            13:45:02.100    0    0ms  pass      token obtained …
  1 ingest           13:45:02.312  212  212ms  pass      task_id=d3…, http=201
  2 redpanda         13:45:02.483  383  171ms  pass      found in tasks.new.<tenant>@4712
  3 pg.investigating 13:45:03.091  991  608ms  pass      state=investigating
  4 pg.completed     13:45:03.472 1372  381ms  pass      terminal=completed
  5 signoz           13:45:04.015 1915  543ms  pass      zovark-api Δ=3, zovark-worker Δ=2
  6 verdict          13:45:04.089 1989   74ms  pass      verdict=benign risk_score=5
  7 cleanup          13:45:04.132 2032   43ms  pass      _probe_cleanup flag set
--------------------------------------------------------------------
  OVERALL: pass  total: 2032ms
```

- `AT` is the wall-clock timestamp when that stage completed.
- `+ms` is cumulative elapsed since Stage 0.
- `HOP` is the elapsed time between this stage and the previous one — this is
  where you read the pipeline's rhythm.

### Exit codes

- `0` — every stage passed end-to-end.
- `1` — at least one stage failed. The `STALL:` line above the overall line
  names the stage and its last observable state.
- `2` — every stage passed but at least one is `degraded` (see below).

### Common STALL lines and what to check

#### `STALL: 1 ingest`
HTTP call to `/api/v1/tasks` failed.
```bash
# API alive?
scripts/stack_healthcheck.sh --skip dashboard,signoz,healer,redpanda,valkey,temporal,postgres
# Is the login working?
curl -fsS -X POST $ZOVARK_API_BASE/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"email":"admin@test.local","password":"TestPass2026"}'
```

#### `STALL: 2 redpanda`
The task_id didn't appear in `tasks.new.<tenant>` within 3 retries.
```bash
# Redpanda container running?
docker ps --filter name=zovark-redpanda
# Can you see any messages on the topic at all?
docker exec -it zovark-redpanda rpk topic consume tasks.new.<TENANT> \
    --offset end --num 20 --format json
# API's redpanda writer connected?
docker compose logs api | grep -i redpanda | tail -20
```
Root cause is usually the API's `redpanda_writer` lost its broker connection
or the topic doesn't exist yet (first-time deploy).

#### `STALL: 3 pg.investigating`
Task row is stuck at `pending` or `queued` after 90 seconds.
```bash
# Worker consuming?
docker compose logs worker | tail -50
# Temporal queue depth — if this is >200 you're at the backpressure soft limit
docker exec -it zovark-temporal tctl --address temporal:7233 tq describe zovark-queue
# Actual row state
docker exec -it zovark-postgres psql -U zovark -d zovark \
    -c "SELECT id, status, task_type, created_at FROM agent_tasks WHERE input->>'probe_id' IS NOT NULL ORDER BY created_at DESC LIMIT 10;"
```
Root causes we've already seen: worker consumer group rebalancing; LLM
semaphore bound to the wrong event loop (audit fix 2.8); Temporal activity
worker saturated at `MAX_CONCURRENT_ACTIVITIES`.

#### `STALL: 4 pg.completed`
Task transitioned out of `pending` but never reached a terminal state.
```bash
# What stage did it get stuck in?
docker exec -it zovark-postgres psql -U zovark -d zovark \
    -c "SELECT task_id, step_number, step_type, status FROM investigation_steps WHERE task_id = '<from STALL line>' ORDER BY step_number;"
# Worker activity panicked?
docker compose logs worker | grep -A 5 -i 'exception\|panic\|failed' | tail -50
```
Most common: tool runner infinite loop (should be fixed by audit 2.5's
ThreadPoolExecutor timeout); assess stage pydantic validation error swallowed
silently (audit 2.18); `probe_noop` plan missing from the worker image
(you forgot to rebuild: `docker compose build worker && docker compose up -d worker`).

#### `STALL: 5 signoz`
Verdict was written but the span count delta is 0.
```bash
# Is the collector receiving spans at all?
docker compose logs zovark-signoz-collector | grep -i 'trace\|span' | tail -20
# Does the worker have the right endpoint set?
docker exec -it zovark-worker-1 env | grep OTEL
# Does ClickHouse have free disk?
docker exec -it zovark-clickhouse clickhouse-client --query "SELECT free_space FROM system.disks"
```
Root causes: `OTEL_EXPORTER_OTLP_ENDPOINT` wrong in worker / api; ClickHouse
full; `OTEL_ENABLED=false`; BatchSpanProcessor flushing slower than the
probe's 2-minute lookback window.

Use `--signoz-required false` to downgrade a Signoz failure to `degraded` if
you're running without the tracing profile.

#### `STALL: 6 verdict`
`output->>'verdict'` is NULL — store stage never wrote the output.
```bash
# Check if the store stage ran at all
docker compose logs worker | grep -i 'store\|notify' | tail -30
# Look at the row directly
docker exec -it zovark-postgres psql -U zovark -d zovark \
    -c "SELECT output FROM agent_tasks WHERE input->>'probe_id' = '<probe_id>';"
```
Root cause is almost always the `_db_conn` context manager in `store.py`
failing its `SET LOCAL app.current_tenant` validation (audit 2.2) — check the
worker logs for `store_investigation: invalid tenant_id`.

### Degraded states

Stage 5 (Signoz) emits `degraded` instead of `fail` when exactly one service's
span count increased but not both. This usually means:

- One side's BatchSpanProcessor hasn't flushed yet (common during cold start).
- One service is misconfigured for OTEL while the other is fine.

Stage 6 (verdict) emits `degraded` when the verdict is non-null but not the
expected `benign`/`5`. This can happen if:

- A non-`probe_noop` task type was somehow matched.
- The `probe_noop` plan was promoted or modified to produce a different verdict.

### 30-day probe-row cleanup

Each probe run creates one row in `agent_tasks`. They're tagged with
`input.synthetic=true` and `input._probe_cleanup=true` so dashboards filter
them out, but operators should prune them periodically:

```sql
DELETE FROM agent_tasks
 WHERE (input->>'synthetic')::boolean
   AND created_at < NOW() - INTERVAL '30 days';
```

Schedule via pg_cron or a k8s CronJob if desired. Not enforced automatically.

### Safe dashboard filter

Task list / investigation views should exclude synthetic probe rows from
default queries:

```sql
WHERE NOT COALESCE((input->>'synthetic')::boolean, false)
```

Include a toggle (`?show_synthetic=1`) for operators who want to see them.

---

## scripts/stack_healthcheck.sh

The existing component-level healthcheck. See `scripts/stack_healthcheck.sh --help`
for the full flag reference.

### Probe order

`api → dashboard → signoz → healer → redpanda → valkey → temporal → postgres`
(plus an optional `e2e` row when `--e2e` is passed).

### Integration with e2e_probe

```bash
scripts/stack_healthcheck.sh --e2e
```

Runs every component probe first, then chains `scripts/e2e_probe.sh --json`
as the final `e2e` row. The probe's exit code rolls up into the healthcheck's
overall exit code: probe exit 0 → pass, probe exit 2 → degraded, anything
else → fail.

Use this from CI as the single "did this deploy actually work?" check.

### Skip lists

When a profile isn't running, pass `--skip` so unused probes don't report
false failures:

```bash
# Dev without Signoz profile
scripts/stack_healthcheck.sh --skip signoz

# Dev without the full siem-lab profile
scripts/stack_healthcheck.sh --skip signoz,telemetry

# End-to-end verification on a stack without tracing
scripts/e2e_probe.sh --signoz-required false
```

---

## Fixture users

The fixture login (`admin@test.local` / `TestPass2026`) that every test,
script, and dashboard hint assumes is seeded by `migrations/seed_dev_data.sql`.
That file is mounted into postgres as `/docker-entrypoint-initdb.d/02-seed-dev.sql`
and runs automatically on a fresh volume after `01-init.sql`.

### Reserved UUIDs

| Entity | UUID | Source |
|---|---|---|
| `SYSTEM` tenant | `00000000-0000-0000-0000-000000000001` | migration 063 |
| `zovark-dev` tenant | `00000000-0000-0000-0000-000000000010` | `seed_dev_data.sql` |
| `admin@test.local` | `00000000-0000-0000-0000-000000000020` | `seed_dev_data.sql` |
| `analyst2@test.local` | `00000000-0000-0000-0000-000000000021` | `seed_dev_data.sql` |

### Verification commands

```bash
# DB-level check — queries postgres directly, exit 0 if all present.
scripts/seed_dev.sh --check

# API-level check — performs a login round-trip, exit 0 on successful auth.
scripts/e2e_probe.sh --check-fixture
```

Both are cheap — under a second on a warm stack. CI runs `--check` before
the integration suite so missing fixtures fail loud with a clear message.

### "Fixture users missing after upgrade"

This happens when an operator upgrades the stack against a **pre-existing**
postgres volume. `docker-entrypoint-initdb.d` only runs on the **first** boot
of an empty data directory, so a new seed file added in an upgrade is not
automatically picked up.

Fix:

```bash
# Re-seed manually. Idempotent — safe to run even if the rows already exist.
scripts/seed_dev.sh

# Verify.
scripts/seed_dev.sh --check

# Confirm API-level auth also works.
scripts/e2e_probe.sh --check-fixture
```

If `--check` reports `tenant missing`, the seed never ran at all. If it
reports `partial fixture`, one of the rows was manually deleted — the re-seed
will restore it without touching the others.

### Rotating the dev password

Dev password is `TestPass2026` (documented in CLAUDE.md). To rotate:

```bash
scripts/seed_dev.sh --regenerate-hash
# Prints an UPDATE snippet. Paste into psql to update the running DB.
# Also manually replace the $2b$12$... literal in migrations/seed_dev_data.sql
# so fresh boots pick up the new value. Two-step by design.
```

### Wiping fixture users

Only needed if you want to test the "seed never ran" diagnostic path.
Re-running the seed restores the rows.

```sql
-- Inside psql:
DELETE FROM users WHERE id IN (
    '00000000-0000-0000-0000-000000000020',
    '00000000-0000-0000-0000-000000000021'
);
DELETE FROM tenants WHERE id = '00000000-0000-0000-0000-000000000010';
```

---

## Signoz probe reports `degraded: ingest empty`

**This is almost always a false alarm.** The signoz probe queries
`/api/v1/services` with a 2-minute lookback window. If the stack has been
idle (no HTTP traffic hitting the API), the window is legitimately empty
and the probe correctly reports `degraded` — but operators read it as
"telemetry broken" and chase a root cause that doesn't exist.

`telemetry-audit-fix` added a **warmup flow** that fixes the common case:
before querying Signoz, `check_signoz` issues one `GET $ZOVARK_API_BASE/ready`
to tickle the API (emitting at least one span via `otelgin.Middleware`), waits
`ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC` seconds (default 3) for the
`BatchSpanProcessor` to flush, and only then queries the services endpoint.

The detail strings now distinguish:

| Detail | Meaning |
|---|---|
| `N service(s) in 2-min window` | Pass — pipeline working. |
| `warmup failure: HTTP <code> (...)` | The API didn't respond to `$ZOVARK_SIGNOZ_WARMUP_URL`. Act on the **API**, not on Signoz. |
| `ingest empty (warmup landed but no services yet — collector→clickhouse?)` | API responded, span was emitted, but Signoz's services list is still empty. Real pipeline bug — check `zovark-signoz-collector` logs for ClickHouse write errors. |
| `ingest empty (no services in 2-min window; warmup skipped)` | `--no-warmup` was passed. Legitimate "nothing recent" state; not a bug. |

### Diagnosing `ingest empty (warmup landed but no services yet)`

```bash
# 1. Is the collector actually receiving spans?
docker compose logs zovark-signoz-collector | grep -iE 'trace|span|error' | tail -30

# 2. Did the schema migrator complete?
docker compose ps zovark-signoz-schema-sync
# Expected: "Exited (0)"  — it runs to completion on first boot, not long-running.

# 3. Does ClickHouse have the signoz_traces database?
docker exec -it zovark-clickhouse clickhouse-client --query 'SHOW DATABASES' | grep signoz

# 4. Manual warmup + direct Signoz query
curl -s http://127.0.0.1:8090/ready   # tickle the API
sleep 5
END=$(date +%s%3N)
START=$(( END - 2*60*1000 ))
curl -s "http://127.0.0.1:3301/api/v1/services?start=$START&end=$END" | jq '.[] | .serviceName'
# Expected: zovark-api in the list
```

Cross-reference `docs/TELEMETRY_ARCHITECTURE.md` for the file:line anchors of
every wiring touchpoint in the pipeline.

---

## Healer probe fails with `curl exit 28` (connection timeout)

**Root cause historically**: the `tecnativa/docker-socket-proxy` allowlist was
too restrictive (`CONTAINERS=1 POST=1` only). The Docker CLI invoked by
`healer.py`'s subprocess calls hit the proxy over `DOCKER_HOST=tcp://docker-socket-proxy:2375`
and got denied on `GET /_ping` + `GET /version` during init handshake.
Compounded by `read_only: true` on the proxy with no writable `/tmp` for
haproxy's pid file, which meant the proxy itself wasn't starting at all.

`telemetry-audit-fix` widens the proxy allowlist by three strictly-read-only
endpoints (`PING=1 VERSION=1 INFO=1`) and adds a `/tmp` tmpfs so haproxy
can write its pid file under `read_only: true`. After the fix:

```bash
# Verify the proxy allows the handshake endpoints
docker compose exec zovark-docker-proxy wget -qO- http://localhost:2375/_ping
# Expected: OK
docker compose exec zovark-docker-proxy wget -qO- http://localhost:2375/version
# Expected: a JSON blob

# Verify the healer's own startup probe succeeded (added in D5)
docker compose logs healer | grep 'docker socket proxy'
# Expected: "[healer] docker socket proxy reachable at tcp://docker-socket-proxy:2375 (attempt 1)"

# If the healer is still timing out:
docker compose logs healer | tail -50
```

The fix requires a container restart because it's a compose-level change:

```bash
docker compose up -d docker-socket-proxy healer
docker compose ps healer   # → Up (healthy)
```

---

## Telemetry probe reports `missing: zovark-worker`

The `telemetry` probe checks that specific service names (default
`zovark-api`,`zovark-worker`) have reported spans in the last 10 minutes.

**Most common cause**: the worker has been idle. No tasks → no spans from
the worker side. Fix: submit a task.

```bash
# Submit one via the e2e probe (also creates a tagged synthetic row — safe)
scripts/e2e_probe.sh

# OR run --prime to force a single login round-trip before probes
scripts/stack_healthcheck.sh --prime
```

**If the worker is still missing after a task submission**: check the worker's
OTel init path.

```bash
docker compose exec zovark-worker-1 env | grep -E 'OTEL|OTLP'
# Must include: OTEL_ENABLED=true and OTEL_EXPORTER_OTLP_ENDPOINT=http://zovark-signoz-collector:4318

docker compose logs worker | grep -iE 'tracer|otel|signoz' | tail -30
# Expected: "[OTEL] trace exporter initialized" or similar

# Check the tracing singleton isn't stuck on the wrong event loop (audit fix 2.8)
docker compose logs worker | grep -iE 'semaphore|different loop' | tail -10
```

**Override the required services list** when your deployment renames the
OTEL service.name for a service:

```bash
ZOVARK_TELEMETRY_REQUIRED_SERVICES=acme-soc-api,acme-soc-worker \
    scripts/stack_healthcheck.sh
```

See `docs/TELEMETRY_ARCHITECTURE.md` for the full architecture reference.

---

## Container discovery reports `skip` for valkey/temporal/postgres

Fixed by `telemetry-audit-fix` D12: `stack_healthcheck.sh` now discovers
container names via `docker compose ps --format json` cached once per
invocation, with a `docker ps --filter "name=zovark-<alias>"` fallback.

If `skip` persists after the fix:

```bash
# Check the discovered map
scripts/stack_healthcheck.sh --debug --skip api,dashboard,signoz,telemetry,healer,redpanda 2>&1 | head -20

# Confirm the containers are actually named as expected
docker ps --format '{{.Names}}' | grep zovark

# Force a specific alias
ZOVARK_COMPOSE_PROJECT=myproj scripts/stack_healthcheck.sh
```

---

## Ingest stall: rows stuck at `pending`

**Symptom**: `scripts/e2e_probe.sh` Stage 3 (`pg.investigating`) times out
with `stuck at 'pending' for 90s`. The API accepted the alert (Stage 1
`ingest` passed) and `rpk topic consume` saw the message land in Redpanda
(Stage 2 `redpanda` passed) — but the worker never picked it up. The
`agent_tasks` row sits at `status='pending'` indefinitely. No Temporal
workflow ever starts.

**Root cause**: `kafka-python` pattern subscription only discovers topics
during the consumer's metadata refresh. The library default for that refresh
interval is **5 minutes** (`metadata_max_age_ms=300_000`). On a fresh stack
with no `tasks.new.<tenant>` topics yet, the worker subscribes to
`^tasks\.new\..+$`, idles, and doesn't notice the API auto-creating the
first tenant topic until up to 5 minutes later — well past the e2e probe's
90-second Stage 3 budget.

**Fix applied** by `fix-e2e-ingest-stall`:

1. `worker/redpanda_consumer.py` sets `metadata_max_age_ms=10000` so
   topic discovery happens within 10 seconds of first publish.
   Override via `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS`.
2. The consumer issues a non-blocking `poll(timeout_ms=0)` immediately after
   `subscribe()` so any pre-existing topics are picked up within the first
   second of worker startup.
3. `scripts/seed_dev.sh` pre-creates `tasks.new.00000000-0000-0000-0000-000000000010`
   (the dev tenant topic) so even a brand-new dev volume has the topic ready
   before the worker starts.
4. The consumer logs `[redpanda] consumer assignment changed: N partition(s)
   across M topic(s)` whenever its set of assigned partitions changes — turns
   silent waiting into an observable signal.

### Diagnostic commands

```bash
# 1. Is the worker's consumer logging assignment changes?
docker compose logs worker | grep -i redpanda
# Expected (after the fix):
#   "Redpanda consumer subscribed pattern=^tasks.new..+$ metadata_max_age_ms=10000"
#   "Redpanda consumer assignment changed partitions=1 topic_count=1 topics=tasks.new.<uuid>"

# 2. List all topics — does the dev tenant topic exist?
docker exec zovark-redpanda rpk topic list
# Expected: tasks.new.00000000-0000-0000-0000-000000000010

# 3. Is the consumer group registered with Redpanda?
docker exec zovark-redpanda rpk group describe zovark-task-workers
# Expected: state=Stable, members=1+, assigned partitions list

# 4. What state are recent probe rows in?
docker exec zovark-postgres psql -U zovark -d zovark -c \
    "SELECT id, status, task_type, created_at FROM agent_tasks WHERE input->>'probe_id' IS NOT NULL ORDER BY created_at DESC LIMIT 5;"

# 5. Force a tighter metadata refresh on the worker (operator escape hatch)
ZOVARK_REDPANDA_METADATA_MAX_AGE_MS=5000 docker compose up -d worker
```

### Re-seeding after a stale volume

If you upgraded an existing stack and the dev tenant topic is missing:

```bash
scripts/seed_dev.sh         # pre-creates the topic (idempotent)
scripts/seed_dev.sh --check # third row should now show the topic as present
scripts/e2e_probe.sh        # should reach Stage 6 within ~10–30 seconds
```

### Related

- `e2e_probe.sh` Stage 3 timeout details now include a state-specific hint:
  - `stuck at 'pending'` → consumer cold start (this section)
  - `stuck at 'queued'` → backpressure (check Temporal queue depth + `ZOVARK_MAX_PENDING_WORKFLOWS`)
- `docs/TELEMETRY_ARCHITECTURE.md` describes the publish→consume path with
  file:line anchors.

---

<a id="api-08p01"></a>

## API returns 500 on POST /api/v1/tasks (08P01 prepared statement collision)

### Symptom

- `scripts/e2e_probe.sh` fails at Stage 0.5 `db_write` (or, if the probe Stage 0.5
  is missing on an old script, at Stage 1 `ingest`) with `http=500`.
- The response body is the sanitised `{"error":"an internal error occurred"}`.
- `rpk topic list` (inside `zovark-redpanda`) shows **zero topics** — no
  `tasks.new.<tenant>` was ever auto-created, because the publish path was
  never reached.
- `docker logs zovark-api` contains **zero** kafka / publish / redpanda lines
  for the failed request, but **does** contain a line of the form:

  ```
  [ERROR] create task record: FATAL: prepared statement name is already in use (SQLSTATE 08P01)
  ```

  or the same `08P01` string referenced from a `probe-db` error log.

### Diagnostic command

```bash
docker logs zovark-api --tail 200 | grep 08P01
```

If that returns one or more lines, this is the failure mode.

### Root cause

The Go API connects to PostgreSQL through PgBouncer in **transaction pooling
mode**. `pgx/v5` defaults to `QueryExecModeCacheStatement`, which issues
**named prepared statements** that get cached on whichever Postgres backend
the API is currently using. Transaction pooling multiplexes a small pool of
real backends across many client connections, so the same backend gets handed
to a different client whose pgx instance also tries to register a statement
with the same auto-generated name. PostgreSQL responds with
`SQLSTATE 08P01 prepared statement … already exists`, a `FATAL` that kills
the connection and surfaces as a 500 in the handler.

This is documented behaviour. The pgx README's "PgBouncer" section is
explicit: *"For PgBouncer in transaction mode, use `QueryExecModeExec`."*

### Fix

The API ships with `ZOVARK_PGX_QUERY_MODE=exec` as the default since the
`fix-pgx-pgbouncer-prepared-stmt` change (April 2026). The startup self-test
in `api/db.go` `selfTestPool()` exercises the same code path and refuses to
boot the API process if the live pool is in a mode that triggers `08P01`.
On a healthy startup you should see both of these lines in `docker logs zovark-api`:

```
pgx_pool_query_mode mode=exec
pgx_pool_self_test result=passed
```

If you see `pgx_pool_self_test_failed`, the API process exited non-zero before
binding the listener — the message names the env var and the safe value.

If you see neither, the API was started against a binary that predates the
fix. Rebuild and restart:

```bash
docker compose build api && docker compose up -d api
```

### Anti-recommendation

**DO NOT** set `ZOVARK_PGX_QUERY_MODE=cache_statement` (or
`QueryExecModeCacheStatement` directly) when `DATABASE_URL` points at
PgBouncer. The startup self-test will catch this and refuse to start the API.
The only legitimate reason to use `cache_statement` is a unit-test or
integration-test stack that connects **directly** to `postgres:5432` without
PgBouncer in front, and even then `exec` works fine.

### Related

- `scripts/e2e_probe.sh` Stage 0.5 `db_write` reproduces this in under one
  second by POSTing to `/api/v1/admin/diagnostics/probe-db` (admin-only),
  which performs an `INSERT INTO probe_writes DEFAULT VALUES RETURNING id`
  followed by a `DELETE` in the same transaction. That endpoint exercises
  the same parameterised round-trip that `POST /api/v1/tasks` uses to write
  to `agent_tasks` — `GET /ready` does not (it's a parameterless `SELECT 1`
  that pgx fast-paths regardless of mode).
- `migrations/070_probe_writes_table.sql` creates the diagnostic table.
- `api/db.go` `parseQueryExecMode` and `selfTestPool` are the implementation.

---

<a id="schema-drift"></a>

## POST /api/v1/tasks returns 500 with 42703 column does not exist (schema drift)

### Symptom

- `POST /api/v1/tasks` returns HTTP 500. The response body is the sanitised
  `{"error":"an internal error occurred"}`.
- `docker logs zovark-api | grep 42703` shows a line of the form
  `[ERROR] create task record: ERROR: column "trace_id" of relation "agent_tasks" does not exist (SQLSTATE 42703)`
  (or a different missing column — `dedup_count`, `path_taken`, etc.).
- `docker logs zovark-api` at startup shows `schema_migrations_check status=absent hint=...`
  at WARN level, OR no `schema_migrations_check` line at all on a binary that
  predates this fix.
- `scripts/e2e_probe.sh` fails at Stage 0.6 `schema_ledger` with detail starting
  with `drift:` or `ledger absent`.
- The previous (`#api-08p01`) failure mode no longer triggers — it was
  masking this one. Once the pgx fix landed, `POST /api/v1/tasks` started
  reaching the schema layer, which is where this drift surfaces.

### Diagnostic

```bash
scripts/apply_migrations.sh --dry-run
```

The script lists every migration on disk that is NOT in the `schema_migrations`
ledger. On a healthy stack this prints `no migrations to apply (everything in
ledger)` and exits 0. On a drifted stack it prints one line per missing file
(typically 14 of them: 054, 055, 059–067, 069, 071) and exits 0 — `--dry-run`
never modifies the DB.

### Fix

```bash
scripts/apply_migrations.sh
```

The runner walks `migrations/*.sql` in numeric-then-lex order, skips any file
already in the ledger, and applies the rest inside isolated transactions. On
the first run against a DB without any ledger, it special-cases migration 072
(`schema_migrations_ledger.sql`) as the bootstrap step — that file creates the
`schema_migrations` table AND backfills 58 rows for the init.sql-frozen era —
then re-scans and applies the rest.

After it completes, restart the API and confirm:

```bash
docker compose restart api
docker logs zovark-api | grep schema_migrations_check
# Expected: schema_migrations_check status=present applied=72
```

### Production strict-mode

For production / customer deploys that should refuse to boot against a drifted
DB, set:

```
ZOVARK_REQUIRE_SCHEMA_LEDGER=true
```

on the API container. Any boot where the ledger query errors or returns 0 rows
will cause `initDB` to fail-fast with the runbook anchor in the error message.
Default is unset (warn-and-continue) so dev workflows like `docker compose down -v && up -d`
don't get gated on a manual migration step.

**DO NOT** set `ZOVARK_REQUIRE_SCHEMA_LEDGER=true` on a dev volume that you
intend to recreate frequently. The expected dev cycle is "wipe volume → init.sql
runs → run apply_migrations.sh → start API"; strict mode would block step 4.

### The 068 SurrealDB cutover gate

`migrations/068_ticket2_surreal_graph_pgvector_retirement.sql` retires the
PostgreSQL entity graph + pgvector tables. It MUST NOT auto-apply until
SurrealDB is live and the entity write path has been migrated. The runner
skips 068 by default and gates it behind `--include-068` + an interactive
`APPLY-068` confirmation. Operators running the standard fix above will see a
"Skipping 068" line and that is correct — leave it alone unless you are
explicitly doing the cutover.

### Related

- The previous failure mode (`08P01` prepared-statement collision) was masking
  this one. See [#api-08p01](#api-08p01) above.
- `migrations/072_schema_migrations_ledger.sql` creates the `schema_migrations`
  table and backfills the init.sql era.
- `scripts/apply_migrations.sh` is the canonical operator workflow.
- `api/db.go` `checkSchemaMigrationsLedger` is the API-side drift check.
- `scripts/e2e_probe.sh` Stage 0.6 `schema_ledger` is the e2e check.


