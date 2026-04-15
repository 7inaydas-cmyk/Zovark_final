## 1. Supersede the in-flight change

- [x] 1.1 Move `openspec/changes/healthcheck-fixes-and-telemetry/` to `openspec/changes/archive/healthcheck-fixes-and-telemetry-superseded-by-telemetry-audit-fix/`
- [x] 1.2 Add a one-line `SUPERSEDED.md` file inside the archived directory explaining that this change (`telemetry-audit-fix`) absorbs it and points at the new change dir

## 2. Audit: verify what the code actually does (read-only)

- [x] 2.1 Confirm `docker-compose.yml` api service line 385-386 sets `OTEL_ENABLED` and `OTEL_EXPORTER_OTLP_ENDPOINT=http://zovark-signoz-collector:4318`; record in the architecture doc
- [x] 2.2 Confirm `docker-compose.yml` worker service line 427-428 sets the same env values
- [x] 2.3 Confirm `docker-compose.yml` healer service line 795-796 sets `ZOVARK_OTEL_ENABLED` + `OTEL_EXPORTER_OTLP_ENDPOINT`
- [x] 2.4 Confirm `api/otel.go` builds a `TracerProvider` with `semconv.ServiceName("zovark-api")`, registers it via `otel.SetTracerProvider`, and exposes `otelgin.Middleware("zovark-api", …)` for the Gin router
- [x] 2.5 Confirm `api/main.go` wires the OTel init BEFORE the pgx/redis init so both get a non-nil `TracerProvider`
- [x] 2.6 Confirm `worker/tracing.py` reads `OTEL_ENABLED` + `OTEL_EXPORTER_OTLP_ENDPOINT`, creates a `TracerProvider` with `service.name=zovark-worker`, attaches an `OTLPSpanExporter` pointing at `/v1/traces` on the collector, and wraps it in a `BatchSpanProcessor`
- [x] 2.7 Confirm `config/signoz/otel-collector-config.yaml` has otlp receivers on 4317/4318, a traces pipeline with processors `[memory_limiter, signozspanmetrics/delta, batch]` and exporter `clickhousetraces` pointing at `tcp://zovark-clickhouse:9000/signoz_traces`
- [x] 2.8 Confirm `docker-compose.yml` has `zovark-signoz-schema-sync` running `signoz/signoz-schema-migrator sync` on first boot, and that `zovark-signoz-collector` has `depends_on: zovark-signoz-schema-sync: service_completed_successfully`
- [x] 2.9 Record every finding as a line in `docs/TELEMETRY_ARCHITECTURE.md` (§3 below)

## 3. docs/TELEMETRY_ARCHITECTURE.md

- [x] 3.1 Create `docs/TELEMETRY_ARCHITECTURE.md` with an ASCII diagram showing the trace flow: `api|worker → OTLP:4318 → zovark-signoz-collector → clickhousetraces → zovark-clickhouse → Signoz query → Signoz frontend`
- [x] 3.2 Add a file:line anchor table for every wiring touchpoint from §2
- [x] 3.3 Add a "How to verify end-to-end" section referencing `scripts/stack_healthcheck.sh --prime` and `scripts/e2e_probe.sh`
- [x] 3.4 Add a "Failure modes and diagnostics" section: (a) healer timeout → socket-proxy allowlist, (b) signoz degraded → cold idle vs broken pipeline, (c) telemetry probe missing service → OTEL_ENABLED or service.name drift, (d) container discovery skip → working directory mismatch
- [x] 3.5 Add a "Re-audit this document on OTel library upgrades" note at the top

## 4. CLAUDE.md correction

- [x] 4.1 Locate the "First-time setup: Run schema migrator …" block in `CLAUDE.md` under `## Observability`
- [x] 4.2 Replace the stale manual instructions with a pointer to `zovark-signoz-schema-sync` in docker-compose.yml; note that the migration is automated and runs on first boot

## 5. docker-compose.yml fixes

- [x] 5.1 Add `PING: 1` to `docker-socket-proxy.environment`
- [x] 5.2 Add `VERSION: 1` to `docker-socket-proxy.environment`
- [x] 5.3 Add `INFO: 1` to `docker-socket-proxy.environment`
- [x] 5.4 Add `tmpfs: ["/tmp:size=4m,noexec,nosuid"]` to `docker-socket-proxy` service
- [x] 5.5 Change `healer.healthcheck.start_period` from `30s` to `60s`
- [x] 5.6 Validate YAML via `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"`

## 6. agent/healer.py startup readiness

- [x] 6.1 Bind the Flask HTTP server on `:8081` BEFORE the Docker SDK init retry loop so `/api/health` is reachable during retries
- [x] 6.2 Add a startup probe function that calls `docker.DockerClient(base_url=os.environ.get('DOCKER_HOST')).ping()` inside a try/except
- [x] 6.3 On failure, log `[healer] docker socket proxy unreachable: <err>` and retry with backoff 2 → 4 → 8 → 16 seconds, capped at 10 attempts (~30 seconds total)
- [x] 6.4 On success, log `[healer] docker socket proxy reachable at $DOCKER_HOST` once
- [x] 6.5 After 10 failed retries, `sys.exit(1)` so compose's `restart: unless-stopped` policy recycles the container

## 7. scripts/stack_healthcheck.sh — container discovery rewrite

- [x] 7.1 Add `_discover_compose_containers` helper that runs `docker compose ps --format json` once and parses both array + NDJSON formats via `jq --slurp`
- [x] 7.2 Populate a `declare -A COMPOSE_MAP` cache keyed on compose service name
- [x] 7.3 Add `_fallback_container_alias <service>` table (postgres → zovark-postgres, redis/valkey → zovark-redis, temporal → zovark-temporal, redpanda → zovark-redpanda, api → zovark-api, worker → zovark-worker-1, healer → zovark-healer, dashboard → zovark-dashboard, signoz → zovark-signoz, signoz-collector → zovark-signoz-collector)
- [x] 7.4 Add `_compose_container_name <service>` that reads the map then falls back to `docker ps --filter "name=$(_fallback_container_alias <service>)"`
- [x] 7.5 Rewrite `_compose_exec_check` to invoke `docker exec <container> <cmd…>` via the resolved name instead of `docker compose exec <service>`
- [x] 7.6 Update `check_valkey`, `check_temporal`, `check_postgres` to resolve their containers via `_compose_container_name` and emit `skip` only when both the map AND the fallback are empty

## 8. scripts/stack_healthcheck.sh — signoz warmup + new flags

- [x] 8.1 Refactor `check_signoz` to (a) `GET /api/v1/health` first, (b) unless `NO_WARMUP=1`, curl `${ZOVARK_SIGNOZ_WARMUP_URL:-$ZOVARK_API_BASE/ready}` once, (c) sleep `${ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC:-3}`, (d) query `/api/v1/services` with a 2-minute lookback window
- [x] 8.2 Emit `pass` when services list is non-empty, `degraded` with detail `ingest empty (no services in 2-minute window)` when empty after a successful warmup
- [x] 8.3 Emit `fail` with detail `warmup failure: HTTP <code>` when the warmup request returns non-2xx — clearly distinct from `ingest empty`
- [x] 8.4 Add `--no-warmup` CLI flag that sets `NO_WARMUP=1`
- [x] 8.5 Add `--prime` CLI flag that invokes `scripts/e2e_probe.sh --check-fixture` before the probe runner starts; on non-zero exit from check-fixture, the script exits with code 1 and a clear error

## 9. scripts/stack_healthcheck.sh — telemetry probe + debug

- [x] 9.1 Append `telemetry` to the `CHECKS` array (after `signoz`, before `healer`)
- [x] 9.2 Implement `check_telemetry` that queries `/api/v1/services?start=<now-10m>&end=<now>`, parses via jq `[.data[]?, .services[]?, .[]?] | …serviceName` to survive Signoz response-shape drift
- [x] 9.3 Read `ZOVARK_TELEMETRY_REQUIRED_SERVICES` (default `zovark-api,zovark-worker`), compare against discovered services
- [x] 9.4 Emit `pass` / `degraded` / `fail` based on all-present / partial / none (same contract as the superseded change's D5)
- [x] 9.5 Add `--debug` flag to the CLI; set `DEBUG=1` and imply `NO_COLOR=1`
- [x] 9.6 When `DEBUG=1`, emit `[debug] container map:` with one line per `COMPOSE_MAP` entry before any probe runs
- [x] 9.7 When `DEBUG=1` and a probe emits `fail` or `degraded`, print the raw HTTP body or `docker exec` stderr indented under the probe row
- [x] 9.8 Update `check_healer` failure detail to include `curl exit N` or `HTTP <code>` instead of a generic `curl failed`
- [x] 9.9 Update `--help` usage text to document `telemetry`, `--debug`, `--prime`, `--no-warmup`, the new env vars (`ZOVARK_SIGNOZ_WARMUP_URL`, `ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC`, `ZOVARK_TELEMETRY_REQUIRED_SERVICES`)

## 10. Runbook updates

- [x] 10.1 Add a "Signoz probe reports `degraded` (ingest empty)" section to `docs/RUNBOOK_HEALTHCHECK.md` explaining cold idle vs broken pipeline, with the `--prime` / `--no-warmup` behaviour
- [x] 10.2 Add a "Healer fails with curl exit 28 (connection timeout)" section pointing at the socket-proxy allowlist fix (§5)
- [x] 10.3 Add a "Telemetry probe reports `missing: zovark-worker`" section explaining the required-services env override
- [x] 10.4 Cross-link to `docs/TELEMETRY_ARCHITECTURE.md` for the architecture reference

## 11. Verification

- [x] 11.1 `bash -n scripts/stack_healthcheck.sh` exits 0 after all edits
- [x] 11.2 `scripts/stack_healthcheck.sh --help` lists `telemetry` in the probe names and documents `--debug`, `--prime`, `--no-warmup`
- [x] 11.3 `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"` succeeds after the compose edits
- [x] 11.4 `scripts/stack_healthcheck.sh --json --skip api,dashboard,signoz,telemetry,healer,redpanda,valkey,temporal,postgres` runs clean with an empty-ish checks array (parse smoke test)
- [x] 11.5 `ZOVARK_API_BASE=http://127.0.0.1:1 scripts/stack_healthcheck.sh --skip api,dashboard,telemetry,healer,redpanda,valkey,temporal,postgres` produces `signoz=fail` with detail containing `warmup failure`
- [x] 11.6 `bash -n agent/healer.py` is not applicable (Python) — instead run `python3 -m py_compile agent/healer.py`
- [x] 11.7 `openspec list` no longer shows `healthcheck-fixes-and-telemetry` in the active changes list (the archival move completed)
- [x] 11.8 Manual live-stack run: `docker compose down && docker compose up -d && scripts/stack_healthcheck.sh --prime` reports every probe `pass` (including healer, signoz, telemetry) within 60 seconds
- [x] 11.9 Manual live-stack run: `docker compose logs healer` after the stack is up shows the `[healer] docker socket proxy reachable at tcp://docker-socket-proxy:2375` line once
- [x] 11.10 Manual live-stack run: `docker compose exec zovark-docker-proxy wget -qO- http://localhost:2375/_ping` returns `OK` (confirming the allowlist expansion works)
