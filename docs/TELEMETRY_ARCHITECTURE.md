# Zovark Telemetry Architecture

> **Re-audit on every OTel SDK upgrade.** This doc is a snapshot of how the
> trace pipeline is wired right now. When you upgrade `opentelemetry-sdk`,
> `otelgin`, `signoz-schema-migrator`, or the signoz-otel-collector image,
> walk every row of the touchpoint table below and verify it still matches.

## TL;DR — the pipeline is simple

```
 ┌────────────────┐     OTLP/HTTP 4318    ┌───────────────────────┐
 │  zovark-api    │────────────────────▶  │ zovark-signoz-        │
 │  (Go)          │                       │ collector             │
 └────────────────┘                       │                       │
                                          │ receivers: otlp       │
 ┌────────────────┐                       │ processors:           │
 │  zovark-worker │────────────────────▶  │   memory_limiter      │
 │  (Python)      │                       │   signozspanmetrics   │
 └────────────────┘                       │   batch               │
                                          │ exporters:            │
                                          │   clickhousetraces    │
                                          └──────────┬────────────┘
                                                     │ tcp 9000
                                                     ▼
                                          ┌───────────────────────┐
                                          │ zovark-clickhouse     │
                                          │ signoz_traces DB      │
                                          └──────────┬────────────┘
                                                     │
                                                     ▼
                                          ┌───────────────────────┐
                                          │ Signoz query service  │
                                          │ + frontend :3301      │
                                          └───────────────────────┘
```

Two Zovark services emit spans — `zovark-api` and `zovark-worker`. Both use
OTLP/HTTP over the internal compose network to reach the collector. The
collector writes traces to ClickHouse via the `clickhousetraces` exporter.
Signoz's query service reads from ClickHouse; the UI queries the query service
via `/api/v1/...` endpoints, which is what `scripts/stack_healthcheck.sh` and
`scripts/e2e_probe.sh` hit.

## Wiring touchpoint table

| Component | File | Line | What it does |
|---|---|---|---|
| API env vars | `docker-compose.yml` | 385 | `OTEL_ENABLED=${OTEL_ENABLED:-true}` |
| API env vars | `docker-compose.yml` | 386 | `OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://zovark-signoz-collector:4318}` |
| Worker env vars | `docker-compose.yml` | 427 | `OTEL_ENABLED=${OTEL_ENABLED:-true}` |
| Worker env vars | `docker-compose.yml` | 428 | `OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://zovark-signoz-collector:4318}` |
| Healer env vars | `docker-compose.yml` | 795 | `ZOVARK_OTEL_ENABLED=${OTEL_ENABLED:-true}` |
| Healer env vars | `docker-compose.yml` | 796 | `OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://zovark-signoz-collector:4318}` |
| API TracerProvider | `api/otel.go` | 119 | `semconv.ServiceName("zovark-api")` |
| API TracerProvider | `api/otel.go` | 127 | `otelTracerProvider = sdktrace.NewTracerProvider(...)` |
| API TracerProvider | `api/otel.go` | 134 | `otel.SetTracerProvider(otelTracerProvider)` |
| API redis instrumentation | `api/otel.go` | 174 | `redisotel.WithTracerProvider(otelTracerProvider)` |
| API Gin middleware | `api/otel.go` | 228 | `otelgin.Middleware("zovark-api", otelgin.WithTracerProvider(otelTracerProvider))` |
| API init ordering | `api/main.go` | 149 | Comment: "OpenTelemetry before DB/Redis so pgx tracer and redisotel see a real TracerProvider" |
| Worker service name | `worker/tracing.py` | 89 | `"service.name": "zovark-worker"` |
| Worker TracerProvider | `worker/tracing.py` | 94 | `provider = TracerProvider(resource=resource)` |
| Worker OTLP exporter | `worker/tracing.py` | 95 | `exporter = OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces")` |
| Worker BatchSpanProcessor | `worker/tracing.py` | 96 | `provider.add_span_processor(BatchSpanProcessor(exporter, ...))` |
| Worker shutdown flush | `worker/tracing.py` | ~110 | `atexit.register(_shutdown_trace_provider)` — ensures queued spans flush on worker exit |
| Collector receivers | `config/signoz/otel-collector-config.yaml` | 5-10 | `otlp.protocols.grpc 0.0.0.0:4317` + `otlp.protocols.http 0.0.0.0:4318` |
| Collector exporter | `config/signoz/otel-collector-config.yaml` | 68-69 | `clickhousetraces: datasource: tcp://zovark-clickhouse:9000/signoz_traces` |
| Collector traces pipeline | `config/signoz/otel-collector-config.yaml` | 90-94 | `receivers [otlp]` → `processors [memory_limiter, signozspanmetrics/delta, batch]` → `exporters [clickhousetraces]` |
| Schema migration | `docker-compose.yml` | 919-927 | `zovark-signoz-schema-sync` runs `signoz/signoz-schema-migrator sync --dsn=tcp://zovark-clickhouse:9000 --up=` on first boot |
| Collector → schema ordering | `docker-compose.yml` | 988-991 | `zovark-signoz-collector` has `depends_on: zovark-signoz-schema-sync: service_completed_successfully` |
| Signoz UI host port | `docker-compose.yml` | (signoz service) | `127.0.0.1:3301:8080` — loopback-only from Batch A |

## How to verify end-to-end

```bash
# 1. Cold-boot the stack.
docker compose down && docker compose up -d

# 2. Wait for API health.
scripts/stack_healthcheck.sh --skip signoz,telemetry,healer
# …should exit 0

# 3. Prime-and-verify the trace pipeline in one command.
scripts/stack_healthcheck.sh --prime
# --prime runs `scripts/e2e_probe.sh --check-fixture` first, which forces a
# login round-trip → at least one otelgin span → worker activity → spans from
# both services land in Signoz.  Then the signoz + telemetry probes run and
# should both report `pass`.

# 4. (Optional) full end-to-end probe with span-count delta verification.
scripts/e2e_probe.sh
# Submits one synthetic alert, tracks it through all 8 stages, and verifies
# the Signoz services list's span count increased for both zovark-api and
# zovark-worker after the alert is processed.
```

## Failure modes

### Signoz probe reports `degraded: ingest empty`

**Meaning**: `/api/v1/services` returned an empty list within the lookback
window. **Almost always a false alarm** on a cold/idle stack.

**Root causes, most to least common**:

1. **Cold idle**: the stack hasn't served any HTTP traffic recently, so no
   spans have been emitted in the last 2 minutes. This is not a bug — it's
   what a healthy pipeline does when no one is using it. Fix: use
   `scripts/stack_healthcheck.sh --prime` which tickles the API first.
2. **BatchSpanProcessor flush delay**: the API emitted a span less than
   ~5 seconds ago but the batch hasn't been flushed. Fix: wait 3–5 seconds
   and re-run (the `--prime` path already includes a `sleep 3`).
3. **Collector → ClickHouse pipeline broken**: this IS a real bug. Check
   `docker compose logs zovark-signoz-collector | grep -i "clickhouse\|error"`.
   ClickHouse disk-full or schema drift shows up here.
4. **`OTEL_ENABLED=false`**: somebody set it in `.env` or the environment.
   Check with `docker compose exec zovark-api env | grep OTEL`.

### Signoz probe reports `fail: warmup failure`

**Meaning**: the warmup `GET $ZOVARK_API_BASE/ready` returned non-2xx. The
API is either down, routing is broken, or the readiness probe is failing.
Distinct from `ingest empty` so you act on the right layer.

### Telemetry probe reports `missing: zovark-worker`

**Meaning**: `zovark-api` is reporting spans but `zovark-worker` hasn't in
the last 10 minutes. Most common cause: the worker has been idle (no tasks
have been submitted). Fix: submit a task via `scripts/e2e_probe.sh` — the
worker will emit spans when it processes the task. If the worker is still
missing after that, check `docker compose logs worker | grep -i "otel\|tracing"`
for init errors (audit 2.8's semaphore-loop-binding fix should be in place).

### Healer probe fails with `curl exit 28`

**Meaning**: the healer HTTP server isn't binding on `:8081`. Almost
always because its Docker SDK init is blocked — the tecnativa/docker-socket-proxy
has a restrictive allowlist and needs `VERSION=1 INFO=1 PING=1` so the SDK's
`ping()` + `version()` handshake can succeed. After `telemetry-audit-fix`
applied, this should be fixed at the compose level. Verify:

```bash
docker compose exec zovark-docker-proxy wget -qO- http://localhost:2375/_ping
# Expected: OK
docker compose exec zovark-docker-proxy wget -qO- http://localhost:2375/version
# Expected: a JSON blob
docker compose logs healer | grep "docker socket proxy reachable"
# Expected: one line confirming the healer's startup probe succeeded
```

### Container discovery reports `skip` for valkey/temporal/postgres

**Meaning**: `docker compose ps -q <service>` returned empty under
working-directory drift, compose project-name drift, or a compose service-key
mismatch. Fix in `telemetry-audit-fix`: `stack_healthcheck.sh` now uses
`docker compose ps --format json` (cached once) and falls back to
`docker ps --filter "name=zovark-<alias>"`. If `skip` persists, run with
`--debug` to see the discovered container map.

## Why we audit this on every OTel SDK upgrade

OpenTelemetry is a young spec. Semantic convention keys change between
versions (`service.name` vs `service_name`, `servicename`, etc.), exporter
HTTP paths change (`/v1/traces` vs `/v1/spans`), and the Signoz collector's
processor list changes between major versions. Any of these can silently
break the pipeline: spans are emitted, received, but dropped at the processor
step because a required attribute key renamed underneath you. The only
defence is a committed reference that a human walks every upgrade.

The three places that have broken historically:

1. `opentelemetry-exporter-otlp-proto-http` changed its `endpoint=` kwarg
   semantics — old versions appended `/v1/traces`, new ones don't. Check
   `worker/tracing.py:95` on every SDK bump.
2. `otelgin.Middleware` signature changed between v0.40 and v0.46 — the
   first argument used to be optional, now it's required. Check
   `api/otel.go:228`.
3. `signoz-schema-migrator` image tag must match the `signoz-otel-collector`
   image tag within a minor version. Check `docker-compose.yml` for both.
