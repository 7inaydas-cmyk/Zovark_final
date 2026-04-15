## Context

The user reported `signoz=degraded` and `healer=fail` on `stack_healthcheck.sh` and asked for a full telemetry-pipeline audit. Before writing this proposal I grepped every relevant file. The findings below are **not hypothetical** — each one cites a concrete file and line from the current working tree.

### Audit verdict: the OTEL wiring is correct

**Environment variables (`docker-compose.yml`):**
- api (line 385-386): `OTEL_ENABLED=${OTEL_ENABLED:-true}`, `OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://zovark-signoz-collector:4318}`
- worker (line 427-428): same values
- healer (line 795-796): `ZOVARK_OTEL_ENABLED=${OTEL_ENABLED:-true}`, `OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://zovark-signoz-collector:4318}`

**Go API instrumentation (`api/otel.go`):**
- line 119: `semconv.ServiceName("zovark-api")`
- line 127: `otelTracerProvider = sdktrace.NewTracerProvider(...)`
- line 134: `otel.SetTracerProvider(otelTracerProvider)`
- line 174: `redisotel.WithTracerProvider(otelTracerProvider)` — redis ops are traced
- line 228: `return otelgin.Middleware("zovark-api", otelgin.WithTracerProvider(otelTracerProvider))` — every HTTP request spawns a span
- `api/main.go` line 149 comment: `OpenTelemetry before DB/Redis so pgx tracer and redisotel see a real TracerProvider (Ticket 9)` — pgx is also instrumented

**Python worker instrumentation (`worker/tracing.py`):**
- line 89: `"service.name": "zovark-worker"`
- line 94: `provider = TracerProvider(resource=resource)`
- line 95: `exporter = OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces")`
- line 96: `provider.add_span_processor(BatchSpanProcessor(...))`

**Collector config (`config/signoz/otel-collector-config.yaml`):**
- receivers: `otlp.protocols.grpc 0.0.0.0:4317`, `otlp.protocols.http 0.0.0.0:4318`
- processors: `memory_limiter`, `signozspanmetrics/delta`, `batch`
- exporters: `clickhousetraces: datasource: tcp://zovark-clickhouse:9000/signoz_traces`
- pipeline: `traces: receivers [otlp] processors [memory_limiter, signozspanmetrics/delta, batch] exporters [clickhousetraces]`

**Schema migration (`docker-compose.yml`):**
- line 919-927: `zovark-signoz-schema-sync` runs `signoz/signoz-schema-migrator:v0.111.34 sync --dsn=tcp://zovark-clickhouse:9000 --up=` on first boot
- line 988-991: the collector depends on both `zovark-clickhouse: service_healthy` AND `zovark-signoz-schema-sync: service_completed_successfully` — correct ordering

So: the pipeline is wired end-to-end correctly, schema migration is automated, the collector waits for the schema to exist. **Nothing is broken on the instrumentation side.**

### Actual root causes

#### Root cause A: healer's Docker SDK init is blocked by the socket-proxy allowlist
Batch B audit 3.6 made the healer route Docker API calls through `docker-socket-proxy` via `DOCKER_HOST=tcp://docker-socket-proxy:2375`. Batch B 3.7 hardened the proxy with `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and `read_only: true`. Both were correct security moves.

But the proxy's environment (line 688-708) is still the minimal allowlist from the original hardening:
```yaml
CONTAINERS: 1
POST: 1
IMAGES: 0
EXEC: 0
...all other endpoints: 0
```

The Python `docker.DockerClient()` constructor issues `GET /_ping` + `GET /version` during init. Both endpoints are denied by the proxy (`PING` and `VERSION` both default to 0). The client retries with increasing backoff and eventually blocks for minutes before failing. During that block, `healer.py` has not yet called `app.run(host='0.0.0.0', port=8081)`, so the compose healthcheck on `curl :8081/api/health` times out — every 30-second interval, every 5-second attempt — and the container is permanently marked unhealthy.

On top of that, `read_only: true` on the proxy container has no writable `/tmp`. tecnativa/docker-socket-proxy uses haproxy internally, and haproxy writes its pid file to `/tmp/haproxy.pid` on startup. Without a writable `/tmp`, haproxy exits non-zero and the proxy itself never starts serving — which would compound the healer block into a "nothing works" scenario.

#### Root cause B: `check_signoz` is a lagging indicator that reports cold idle as `degraded`
`scripts/stack_healthcheck.sh` `check_signoz` (around line 310-345):
```bash
end_ms=$(_now_ms)
start_ms=$(( end_ms - 10 * 60 * 1000 ))
... curl /api/v1/services?start=${start_ms}&end=${end_ms} ...
if services_count >= 1: emit pass
else: emit degraded "health OK but trace ingestion idle"
```

The `/api/v1/services` endpoint returns services that have reported at least one span within the requested window. After 10 minutes of idle, the window legitimately contains zero services, not because Signoz is broken but because nothing has been traced. The probe confuses "no recent spans" with "pipeline dark", producing a `degraded` row that operators read as a failure.

#### Root cause C: container-name discovery via `docker compose ps -q <service>` is fragile
`_compose_present` (line 261-270) calls `docker compose ps -q postgres` which depends on `COMPOSE_PROJECT_NAME`, the current working directory, and the compose service-name key matching. When any of those drift, the probe emits `skip` for valkey, temporal, and postgres — which is the wrong signal, because the containers are actually running.

### Why one change instead of two

The pending `healthcheck-fixes-and-telemetry` change (0/41 tasks, unapplied) already proposes fixes for A and C plus most of the scaffolding for a `telemetry` probe. Shipping this audit as a separate change would produce two PRs that touch the same files (`stack_healthcheck.sh`, `docker-compose.yml`, `healer.py`, `CLAUDE.md`) with overlapping spec deltas. That guarantees merge conflict and operator confusion.

Instead, this change **supersedes** `healthcheck-fixes-and-telemetry`:
- Folds its 41 tasks into tasks.md §§1–6.
- Absorbs its spec delta into `specs/stack-healthcheck/spec.md`.
- Adds the four new audit findings from this round:
  1. Warmup before querying Signoz services (§B fix)
  2. Committed telemetry architecture doc so future audits don't retrace this work
  3. Correct the stale schema-migrator instruction in CLAUDE.md
  4. `--prime` flag for cold-start verification in one invocation

Stakeholders: everyone who runs `stack_healthcheck.sh` after `docker compose up -d`; CI's "Stack healthcheck" step; operators doing post-upgrade verification.

## Goals / Non-Goals

**Goals:**
- **Zero lying rows.** Every probe's status matches the underlying reality of the pipeline, not a stale 10-minute window.
- **One coherent deliverable** — audit findings + all fixes in a single apply-able change.
- **Ship a working healer**, a passing signoz probe on a cold stack, and correct `valkey`/`temporal`/`postgres` detection without a manual workaround.
- **Make the wiring auditable from the repo alone** via a committed `docs/TELEMETRY_ARCHITECTURE.md` with file:line anchors.
- **Keep every safety property** from Batch B 3.6/3.7 — socket-proxy stays, docker.sock bind stays gone, `cap_drop: [ALL]` stays, `read_only: true` stays (with the tmpfs addition).
- **No new dependencies.** `jq`, `curl`, `docker`, `bash` — everything is already in the operator's toolbelt.

**Non-Goals:**
- Not re-architecting OTel. The wiring is correct; we're auditing it, not changing it.
- Not touching the Signoz collector config pipeline (receivers / processors / exporters are correct).
- Not touching `zovark-signoz-schema-sync` or `zovark-clickhouse` — both are correctly configured.
- Not adding Prometheus scrape endpoints or Grafana dashboards (out of scope; the audit was about OTel traces, not metrics).
- Not rewriting the healer's fundamental architecture. The fix is config-level: widen the socket-proxy allowlist by three read-only endpoints, add a tmpfs, bump the healthcheck start_period, add one startup log line.
- Not building a metric-ingestion probe. The `telemetry` probe verifies traces only. Metrics have their own path and are a follow-up.
- Not replacing `stack_healthcheck.sh` with `zvadmin diagnose`. Two tools, two audiences (bash script for the common path; Go binary for deep inspection).
- Not attempting to make `signoz` probe pass without issuing ANY traffic to the stack. Operators who want a pure read-only probe pass `--no-warmup`.

## Decisions

### D1 — Supersede, don't compete
This change **archives `healthcheck-fixes-and-telemetry` without applying** and re-homes its 41 tasks into this change's tasks.md. The spec delta is merged into `specs/stack-healthcheck/spec.md` with one consolidated set of MODIFIED and ADDED requirements. Rationale: two changes touching the same files is a merge-conflict factory.

### D2 — Socket-proxy allowlist: add `VERSION=1 INFO=1 PING=1`
Inherited verbatim from `healthcheck-fixes-and-telemetry` D6. Three new env vars in the `docker-socket-proxy` service. All three endpoints are read-only per the tecnativa image docs. No write surface is added. Together they let the Python Docker SDK's `ping()` + `version()` calls succeed during `DockerClient()` init, unblocking the healer's startup sequence.

### D3 — Socket-proxy `tmpfs: /tmp`
Inherited from `healthcheck-fixes-and-telemetry` D7. Add `tmpfs: ["/tmp:size=4m,noexec,nosuid"]` to the `docker-socket-proxy` service so haproxy's pid file can land somewhere under the `read_only: true` filesystem. `noexec` keeps the defence-in-depth posture.

### D4 — Healer healthcheck start_period 30s → 60s
Inherited from `healthcheck-fixes-and-telemetry` D8. Gives the healer's `DockerClient()` init time to complete on slow runners without tripping the compose healthcheck.

### D5 — Healer startup readiness log + exponential backoff
Inherited from `healthcheck-fixes-and-telemetry` D10. One new block in `healer.py` before `app.run(...)`: probe `http://$DOCKER_HOST/_ping` via the Python Docker SDK, retry 2→4→8→16 seconds up to 10 attempts, log clear success/failure lines, `sys.exit(1)` after persistent failure so compose `restart: unless-stopped` recycles the container. Importantly, **bind the HTTP server BEFORE the retry loop** so `:8081/api/health` is reachable even while the Docker init is still looping — preventing the healthcheck from timing out during legitimate retry windows.

### D6 — `check_signoz` warmup: probe `$ZOVARK_API_BASE/ready` before querying Signoz
The new flow inside `check_signoz`:
```bash
# 1. Probe signoz /api/v1/health — still the first gate
# 2. Unless --no-warmup: curl $ZOVARK_API_BASE/ready once, to emit a span via otelgin.Middleware("zovark-api")
# 3. Sleep 3 seconds — BatchSpanProcessor flush (default schedule_delay_millis=5000 → give it room)
# 4. Query /api/v1/services with a 2-minute window (not 10)
# 5. If the list contains ≥1 service, emit pass. Empty → degraded.
```
Rationale: a healthy pipeline will now emit at least one span per run, so cold idle no longer produces spurious `degraded`. A probe that triggers a real `GET /ready` and gets a passing span back is far more informative than one that passively queries the lagging indicator.

`--no-warmup` preserves the original read-only behaviour for callers that want a pure-observation probe.

**Alternative considered:** shorten the window to 60 seconds and leave the probe read-only. Rejected — still fails on idle stacks between probe runs, still produces false `degraded`, and the operator experience is "the probe is flaky, ignore it" — which is the same anti-pattern we're trying to fix.

**Alternative considered:** query Signoz ClickHouse directly for recent span count. Rejected — requires a ClickHouse client binary and password-in-script, more fragile than the HTTP path.

### D7 — Add `--prime` as an explicit opt-in for cold-start verification
`scripts/stack_healthcheck.sh --prime` runs `scripts/e2e_probe.sh --check-fixture` BEFORE the probes start. That forces a real login round-trip, which tickles the API and emits several spans. Combined with D6's warmup-in-probe, this means a freshly-booted stack reports `signoz=pass, telemetry=pass` on the first `--prime` run.

`--prime` is a distinct flag from `--e2e` because:
- `--e2e` creates a task row (side effect on `agent_tasks`)
- `--prime` only does a login + `/ready` fetch (no task row, no side effect on primary tables)

### D8 — `--no-warmup` escape hatch
Any caller that wants `check_signoz` to stay purely observational — e.g. a CI step that runs the probe in a loop and doesn't want to tickle the API every iteration — can pass `--no-warmup`. Default is warmup on.

### D9 — Separate `telemetry` probe from `signoz` probe
Inherited from `healthcheck-fixes-and-telemetry` D3. Two distinct probes:
- `signoz` → "Signoz frontend + ClickHouse ingest pipeline is up, with at least one service reporting (warmup path included)"
- `telemetry` → "The specific required services (`zovark-api`, `zovark-worker` by default) have reported spans — the pipeline is verified end-to-end for the services we care about"

A fresh `docker compose up -d` with warmup will now have `signoz=pass` within 3 seconds. `telemetry=degraded` is still possible if only the API was tickled and the worker hasn't run anything — `--prime` fixes that too.

### D10 — Ship `docs/TELEMETRY_ARCHITECTURE.md` with file:line anchors
A committed document that explains the full trace flow with file:line references so the next operator's audit takes 5 minutes instead of 2 hours. Structure:
1. Diagram (ASCII): API / Worker → OTLP 4318 → collector → ClickHouse → Signoz UI.
2. Table: every wiring touchpoint with file:line anchor.
3. Failure modes and diagnostic commands.
4. "Why we audit this document on every OTel library upgrade" note.

### D11 — Fix CLAUDE.md's stale claim about manual schema-migrator
CLAUDE.md currently says "First-time setup: Run schema migrator once after ClickHouse starts: docker run --rm signoz/signoz-schema-migrator …". That was true pre-rebrand; it hasn't been true since the Signoz v0.76 upgrade added `zovark-signoz-schema-sync` to compose. Rewrite the section to reflect current behaviour and point at the automated service.

### D12 — Container-name discovery (inherited from healthcheck-fixes-and-telemetry D1/D2)
- `_discover_compose_containers` runs `docker compose ps --format json` once per invocation, parses via `jq --slurp` (handles both newline-JSON and JSON-array compose output formats), populates a `declare -A COMPOSE_MAP` cached map.
- `_compose_container_name <service>` reads the map first, falls back to `docker ps --filter "name=zovark-<alias>"` when the map misses, finally emits empty.
- `_compose_exec_check` uses `docker exec <container-name>` directly instead of `docker compose exec <service>`. Works regardless of compose project name or working directory.

## Risks / Trade-offs

- **[Risk] Adding `VERSION/INFO/PING` to socket-proxy expands attack surface.** → Accepted: all three are documented-safe read endpoints per tecnativa image docs. No write paths, no exec, no network/volume/image surface.
- **[Risk] `tmpfs /tmp` shadows the image's existing `/tmp`.** → Accepted: empty at runtime, haproxy is the only consumer. `noexec` keeps the defence-in-depth.
- **[Risk] `check_signoz --prime`/warmup adds one API call per probe run, which might be undesired in a read-only CI.** → Mitigation: `--no-warmup` opt-out documented in `--help`.
- **[Risk] The warmup path issues a `GET /ready` which itself is an API call — if the API is down, warmup fails and signoz probe emits `degraded` with a warmup-failure detail.** → Accepted: that's a legitimate failure mode. The distinct detail string lets operators see "warmup failed" vs "ingest empty" on the table row.
- **[Risk] `docker compose ps --format json` output format varies across compose versions.** → Mitigation: `jq --slurp` + the `if type == "array" then .[] else . end` pattern handles both. Fall back to `docker ps --filter` on any parse failure.
- **[Risk] Superseding `healthcheck-fixes-and-telemetry` loses the spec history for anyone grep'ing the openspec directory.** → Mitigation: this change's proposal.md explicitly names the superseded change, and the archival flow will leave it in `openspec/changes/archive/` for reference.
- **[Trade-off] This change is larger than a single-audit change because it folds in 41 tasks from a pending change.** → Accepted: one merge beats two, and the review is faster than the two-PR alternative.
- **[Trade-off] `docs/TELEMETRY_ARCHITECTURE.md` will drift as OTel libraries upgrade.** → Accepted: we note at the top of the doc that it must be re-audited on every OTel SDK upgrade, same expectation as any committed architecture doc.

## Migration Plan

1. **Archive the superseded change**: `mv openspec/changes/healthcheck-fixes-and-telemetry openspec/changes/archive/healthcheck-fixes-and-telemetry-superseded-by-telemetry-audit-fix` as the first task. Adds a tiny note file explaining the supersedence. This happens in-place during the apply step.
2. **Single PR, no flags**: land `stack_healthcheck.sh` rewrite, `docker-compose.yml` socket-proxy allowlist + tmpfs + healer start_period, `healer.py` startup readiness, `CLAUDE.md` fix, new `docs/TELEMETRY_ARCHITECTURE.md`, new runbook sections, all at once.
3. **Post-merge verification**:
   - `docker compose down && docker compose up -d` (no `-v` — keep volumes)
   - `docker compose ps` → healer `Up (healthy)`, not `Up (unhealthy)`
   - `scripts/stack_healthcheck.sh` → every probe `pass` within 60 seconds
   - `scripts/stack_healthcheck.sh --prime` → also passes, with the first run showing `signoz=pass` even on a freshly-booted stack
4. **Rollback**: `git revert`. Zero runtime impact beyond the socket-proxy endpoint allowlist, which is a config-level change.

## Open Questions

- **Q1**: Should `--prime` run `e2e_probe.sh --check-fixture` or issue a dedicated warmup request? → **Recommendation**: `--check-fixture` because it already exists and is explicitly designed for "verify login works" without the full pipeline side effects. Reuse beats reinvention.
- **Q2**: Should the warmup request in `check_signoz` be configurable via env? → **Recommendation**: yes, `ZOVARK_SIGNOZ_WARMUP_URL` defaults to `$ZOVARK_API_BASE/ready`. Overrideable when the API is gated behind an auth wall.
- **Q3**: Should the `telemetry` probe also warmup the worker (not just the API)? → **Recommendation**: no — that requires submitting a task, which is what `--e2e` is for. The `telemetry` probe verifies recent worker activity; if the worker has been idle for 10 minutes, `--prime` or `--e2e` is the right tool, not a separate warmup.
- **Q4**: Should we add a span-freshness threshold (e.g. "last span < 60s ago") to the `telemetry` probe? → **Recommendation**: defer. Current "service in last 10 min" is enough for the common case. Span-age thresholds are operator-tunable and add scope beyond this change.
- **Q5**: Should `docs/TELEMETRY_ARCHITECTURE.md` include a rendered SVG or just ASCII? → **Recommendation**: ASCII. Committed text, diffable, no tooling required.
