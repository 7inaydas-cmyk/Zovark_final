## Why

The user reported `stack_healthcheck.sh` showing `signoz=degraded` and `healer=fail` and asked for a full telemetry-pipeline audit. A pre-proposal static audit of every relevant file found that **the OTEL wiring itself is completely correct** — the red rows are lying about the underlying state of the pipeline. The audit surfaced three concrete root causes, none of which are "telemetry is broken":

1. **Healer's Docker SDK init is blocked by the socket-proxy allowlist.** Batch B's audit 3.6 routed healer through `docker-socket-proxy` via `DOCKER_HOST=tcp://docker-socket-proxy:2375`, which was the right security call. But Batch B's 3.7 left the proxy allowlist at `CONTAINERS=1 POST=1` only. The Python Docker SDK's `DockerClient()` constructor blocks on `GET /_ping` → `GET /version` — both denied by the proxy — so `healer.py`'s init hangs before reaching `app.run(host='0.0.0.0', port=8081)`. The compose healthcheck on `curl :8081/api/health` times out, and every restart just re-hangs. Additionally, Batch B 3.7's `read_only: true` on the proxy breaks haproxy's startup because there's no writable `/tmp` for the pid file — making this more than a "slow to boot" issue.

2. **`stack_healthcheck.sh check_signoz` is a lagging indicator.** The probe queries `/api/v1/services?start=<now-10m>&end=<now>` and emits `degraded` when the list is empty. But that list only contains services that reported spans within the last 10 minutes. On a **cold stack that hasn't taken any HTTP traffic yet**, Signoz is correctly up, ClickHouse is correctly writing, the collector is correctly exporting — and the answer is still legitimately "0 services in the last 10 minutes" because no work has happened. The probe reports this as `degraded`, operators read it as "telemetry broken", and chase a root cause that doesn't exist.

3. **Container-name discovery in `_compose_present` is fragile.** `docker compose ps -q <service>` returns empty when the working directory, `COMPOSE_PROJECT_NAME`, or compose service-name key drift from the script's assumptions. That's why `valkey`, `temporal`, and `postgres` show `skip` when the containers are actually up and running.

The OTEL wiring verification at the top of this audit is the most important delivery: a permanent artifact (committed at `docs/TELEMETRY_ARCHITECTURE.md`) that says exactly what the pipeline looks like, with file-path-and-line-number references, so the next operator who sees a `degraded` row doesn't chase the same ghost.

**This change supersedes the pending `healthcheck-fixes-and-telemetry` change** (0/41 tasks, unapplied). That change correctly identified #1 and #3 above but proposed them as a separate deliverable, which would have produced two partially-overlapping PRs. This change folds those 41 tasks in, adds the four new findings from the fresh audit (#2 lagging indicator, tickle-to-prime, schema-sync verification, tracelink telemetry doc), and ships as one coherent deliverable.

## What Changes

### Fold in every task from the pending `healthcheck-fixes-and-telemetry` change
- **Container-name discovery** via `docker compose ps --format json` cached into `COMPOSE_MAP`, with a `docker ps --filter "name=zovark-<alias>"` fallback table (`postgres→zovark-postgres`, `redis/valkey→zovark-redis`, `temporal→zovark-temporal`, …).
- **Direct `docker exec <container>`** for valkey/temporal/postgres probes instead of `docker compose exec <service>`.
- **New `telemetry` probe** (separate from `signoz`) that asserts `zovark-api` AND `zovark-worker` are both in the Signoz services list, with `ZOVARK_TELEMETRY_REQUIRED_SERVICES` env override.
- **`--debug` flag** prints the container map, raw HTTP response bodies on failure, and captured stderr from `docker exec` probes.
- **Healer probe detail** names the curl exit code / HTTP status explicitly on failure.
- **Socket-proxy allowlist**: add `VERSION=1 INFO=1 PING=1` so the Docker SDK init handshake works.
- **Socket-proxy tmpfs**: add `tmpfs: ["/tmp:size=4m,noexec,nosuid"]` so haproxy's pid file has a writable location under `read_only: true`.
- **Healer `start_period`** bumped from 30s to 60s.
- **`healer.py` startup readiness log** — one-shot probe of `DOCKER_HOST` with exponential backoff retry (2s → 4s → 8s → 16s, max 10 attempts), clear log line on success, `sys.exit(1)` after persistent failure so compose `restart: unless-stopped` recycles the container.
- **`docs/RUNBOOK_HEALTHCHECK.md`** sections for the three failure modes (`skip`, healer timeout, telemetry degraded).

### NEW items from this audit

- **`docs/TELEMETRY_ARCHITECTURE.md` (new)** — a diagrammed explanation of how a span flows from `zovark-api` / `zovark-worker` → OTLP:4318 → `zovark-signoz-collector` → `clickhousetraces` exporter → `zovark-clickhouse` → Signoz query service → Signoz frontend. References the exact files and line numbers:
  - `api/otel.go:119` `semconv.ServiceName("zovark-api")`, `:127` `NewTracerProvider`, `:134` `otel.SetTracerProvider`, `:228` `otelgin.Middleware("zovark-api", …)`, `:174` redis instrumentation, pgx tracer init in `api/db.go`
  - `worker/tracing.py:89` `service.name=zovark-worker`, `:94` `TracerProvider`, `:95` `OTLPSpanExporter(endpoint=.../v1/traces)`, `:96` `BatchSpanProcessor`
  - `docker-compose.yml` lines where api / worker / healer receive `OTEL_ENABLED` and `OTEL_EXPORTER_OTLP_ENDPOINT`
  - `config/signoz/otel-collector-config.yaml` receivers, processors, exporters, `service.pipelines.traces`
  - `docker-compose.yml` `zovark-signoz-schema-sync` service — automated schema migration on first boot, NOT a manual step (corrects CLAUDE.md's stale claim)
  - Every known "the pipeline is dark" failure mode and its diagnostic path

- **Fix CLAUDE.md claim that schema-sync is manual**. The line under `## Observability` says "First-time setup: Run schema migrator once after ClickHouse starts: docker run --rm signoz/signoz-schema-migrator…". That's wrong — `zovark-signoz-schema-sync` has done this automatically since the Signoz v0.76 upgrade. Rewrite to reflect reality.

- **Fix the `check_signoz` lagging-indicator behavior.** Currently: any empty `/api/v1/services` response maps to `degraded`. New behavior: the probe first issues a small warmup request to `$ZOVARK_API_BASE/ready` (which is already traced via `otelgin.Middleware`) to emit at least one span from the API side, waits 3 seconds for the BatchSpanProcessor to flush, THEN queries `/api/v1/services`. This guarantees that a healthy pipeline reports `pass` on the first run, not after a 10-minute warmup. The probe still emits `degraded` if the services list is empty after the warmup, which is a legitimate signal (the span was emitted but didn't land — real pipeline bug). A new `--no-warmup` flag preserves the old behavior for callers that want a read-only probe.

- **Verify `zovark-signoz-schema-sync` completed successfully before the collector starts.** Add a compose-level `depends_on: condition: service_completed_successfully` on `zovark-signoz-collector` → `zovark-signoz-schema-sync`. Without this, a first-boot race can start the collector before the ClickHouse tables exist, causing span exports to fail silently until the collector retries. (Grep confirmed the collector already `depends_on: zovark-clickhouse: service_healthy` and `zovark-signoz-schema-sync: service_completed_successfully` on line 988-991 — this is ALREADY in place. The change here is documenting that it's in place so future audits don't re-chase it.)

- **Add a `--prime` flag to `scripts/stack_healthcheck.sh`** that POSTs one dummy alert (via the existing `scripts/e2e_probe.sh --check-fixture` path) before running probes, so `telemetry` reports `pass` on the first run against a cold stack. `--prime` is opt-in because, like `--e2e`, it has a mild side effect (one login round-trip).

- **Fix the `signoz` probe's detail field** to distinguish "probe-side warmup failed" from "Signoz saw no services" so operators can act on the right root cause.

## Capabilities

### New Capabilities

- `telemetry-architecture-docs`: A committed reference document at `docs/TELEMETRY_ARCHITECTURE.md` that explains the full span flow with file:line anchors and names every known failure mode.

### Modified Capabilities

- `stack-healthcheck` — owned by the `stack-healthcheck-script` change (already applied) and extended by the now-superseded `healthcheck-fixes-and-telemetry` change:
  - Adds every requirement from `healthcheck-fixes-and-telemetry`: dynamic container discovery, fallback table, direct `docker exec`, telemetry probe, `--debug` flag, healer detail on failure, socket-proxy allowlist + tmpfs fixes, `check_signoz` stops owning service-specific assertions.
  - ADDS: `check_signoz` warmup-and-flush before querying services; `--prime` flag; `--no-warmup` escape hatch; signoz detail field distinguishes warmup failure from ingest empty.
- `e2e-pipeline-probe` — owned by the `e2e-pipeline-probe` change (already applied). No requirement changes. The probe's existing `--check-fixture` flag is reused as the warmup path for `stack_healthcheck.sh --prime`.

## Impact

- **Supersedes** `openspec/changes/healthcheck-fixes-and-telemetry/` — that change can be archived without applying. Its 41 tasks are folded into this change (tasks.md §§1–6). Its spec delta is absorbed into this change's spec delta (`specs/stack-healthcheck/spec.md`).
- **Affected code**: `scripts/stack_healthcheck.sh` (rewrite of container discovery + new `telemetry`, `--debug`, `--prime`, `--no-warmup` flags, warmup-then-query in `check_signoz`), `docker-compose.yml` (socket-proxy allowlist + tmpfs + healer start_period), `agent/healer.py` (startup readiness + exponential backoff), `CLAUDE.md` (fix the stale schema-migrator instruction), new `docs/TELEMETRY_ARCHITECTURE.md`, new `docs/RUNBOOK_HEALTHCHECK.md` sections.
- **Runtime impact**: zero on core services. Socket-proxy gains three read-only endpoints. Collector pipeline is unchanged (it's already correct). The healthcheck script's warmup adds one `GET /ready` per run.
- **Risk**: low. The socket-proxy allowlist additions are strictly read-only. The signoz-probe warmup is opt-out via `--no-warmup`. The container-discovery change is additive with a fallback to the existing behavior. The documentation additions are purely informational.
- **Breaking**: none. Every runtime change is additive or a configuration relaxation.
