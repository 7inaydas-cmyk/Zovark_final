## Context

The live first-run of `scripts/stack_healthcheck.sh` against the Batch B stack produced this output:

```
PROBE         STATUS     LATENCY  DETAIL
api          ✓  pass            12ms  matched: .status == "ready"
dashboard    ✓  pass             8ms  HTTP 200 (2B)
signoz       ✓  pass           43ms  health OK, 1 service(s) in last 10 min
healer       ✗  fail         5003ms  curl failed: (28) Operation timed out
redpanda     ✓  pass             3ms  tcp 127.0.0.1:19092 open
valkey       -  skip            0ms  container 'redis' not running
temporal     -  skip            0ms  container 'temporal' not running
postgres     -  skip            0ms  container 'postgres' not running
OVERALL: fail
```

All three skipped containers are actually running. The `docker compose ps -q <service>` call in `_compose_present` returns empty because the test environment has compose in a subdirectory / non-default project name / working-directory drift. This is a real bug, not a misread.

The `healer` timeout is a direct regression from Batch B 3.6+3.7: we routed healer through `docker-socket-proxy` but the proxy's allowlist doesn't expose the endpoints the healer needs, and we marked the proxy `read_only: true` which breaks its internal haproxy.

The `signoz` pass is technically correct (the Signoz frontend is alive) but it is a liar: the single service it saw in the last 10 minutes was **Signoz's own self-instrumentation**, not any Zovark service. The script needs to call the pipeline instrumented end-to-end, not just "Signoz is up".

Stakeholders: every contributor who runs the script after `docker compose up -d`; the CI "Stack healthcheck" step from the previous change; operators who use `scripts/stack_healthcheck.sh` as their first-line triage tool.

## Goals / Non-Goals

**Goals:**
- **Stop lying about `skip`**. When a probe says `skip`, the container is genuinely not running. When it's running but the script can't find it, that's a bug in the script, not an operator problem, and the probe must emit `fail` (or pass if it can find the container via a fallback).
- **Restore healer health** without regressing the Batch B security fix. Socket proxy stays, docker.sock bind stays gone; we only enlarge the allowlist by three read-only endpoints and fix the tmpfs.
- **Verify end-to-end telemetry**, not just "Signoz is up". A new `telemetry` probe fails the run when `zovark-api` or `zovark-worker` is not currently emitting spans.
- **Keep the script self-contained**. No Python, no `gh`, no new packages. `jq` stays the only non-core dependency.
- **Preserve the exit-code contract** from the previous change: 0=pass, 1=fail, 2=degraded.

**Non-Goals:**
- Not redesigning the probe runner or the table renderer. The existing structure is fine.
- Not adding retry loops to `telemetry`. One shot per probe, same as every other check.
- Not adding a `--wait` mode. Operators can still loop the script if they need to wait-for-green.
- Not touching the Signoz collector config, the collector->clickhouse pipeline, or the worker/api OTEL initialization. The audit already covered those; this change verifies them, it doesn't reshape them.
- Not handling non-docker-compose deployments (pure docker run, podman, k8s). The script is explicitly a compose helper.
- Not diagnosing the healer via code changes to `healer.py` beyond adding a startup readiness log line. The real fix is socket-proxy config, not healer logic.
- Not adding a distinct "api-worker telemetry" probe per service. One `telemetry` probe with a configurable required-services list is enough.

## Decisions

### D1 — Discover containers via `docker compose ps --format json` + jq, once, cached at script start
Move the dependency on compose project name / working directory out of every probe and into a single discovery call at the top of `main()`. Cache the result in an associative array `COMPOSE_MAP[<service>]=<container_name>`. Every `_compose_exec_check` call looks up the container via the cache, not a live `docker compose ps -q` query.

Fall back to `docker ps --filter "name=zovark-<alias>"` when compose json is empty or returns an error — this catches the "operator ran the script from the wrong directory" case. The alias table is small and explicit:
```
postgres → zovark-postgres
redis / valkey → zovark-redis
temporal → zovark-temporal
redpanda → zovark-redpanda
api → zovark-api
worker → zovark-worker-1 (docker-compose scaling suffix)
healer → zovark-healer
dashboard → zovark-dashboard
signoz → zovark-signoz
signoz-collector → zovark-signoz-collector
```

**Alternative considered:** `docker compose -f docker-compose.yml ps` — forces the script to know where the compose file lives. Rejected — adds a `--compose-file` flag and still fails when the user shares the shell from a different repo.

**Alternative considered:** `docker ps --format '{{.Names}}'` and regex-match by name prefix. Rejected as primary because it doesn't know the service→container mapping (e.g. `valkey` probe must hit the `zovark-redis` container, not a container named `valkey`). Kept as **fallback**.

### D2 — `_compose_exec_check` uses `docker exec` not `docker compose exec`
Once we have the container name in hand, `docker exec -i <container_name> <cmd...>` is a direct call that doesn't need the compose CLI or the compose file at all. This is both faster and more portable. `-T` (no TTY) stays a `docker compose exec` flag; `docker exec` already defaults to no TTY so we drop the flag.

**Alternative considered:** keep `docker compose exec` and only fix the discovery path. Rejected — if the discovery path needs `docker compose ps --format json` to work, the `docker compose exec` path already has the same working-directory dependency and would still fail.

### D3 — New `telemetry` probe is SEPARATE from `signoz`
Both probes stay in the default `CHECKS` array. `signoz` remains "Signoz frontend is healthy". `telemetry` is "Zovark services are emitting spans into Signoz right now". The distinction matters:

- A fresh `docker compose up -d` legitimately shows `signoz=pass, telemetry=degraded` for the first 60 seconds before the worker has emitted its first span. That's a cold start, not a regression.
- A broken OTEL collector → ClickHouse pipeline shows `signoz=pass, telemetry=fail`. That's a real incident.
- A stopped Signoz shows `signoz=fail, telemetry=fail`. Both probes report, operators see the root cause on the first line.

### D4 — `telemetry` probe required-services list is configurable
Default is `zovark-api,zovark-worker`. Override via `ZOVARK_TELEMETRY_REQUIRED_SERVICES` (comma-separated). This accommodates:
- Customer deployments that rename `zovark-api` → `acme-soc-api` via `OTEL_SERVICE_NAME`.
- The `air-gapped` profile that runs only a worker and no API.
- Dev flows where a contributor only wants to verify one service is instrumented.

**Alternative considered:** auto-derive required services from the compose file. Rejected — the mapping from compose service name to OTEL `service.name` attribute is loose and depends on how the service initializes its tracer.

### D5 — `telemetry` degraded vs fail
- **pass**: every service in the required list is present in the last-10-min services list.
- **degraded**: at least one but not all required services are present. Detail: `missing: <csv of absent services>`.
- **fail**: none of the required services are present. Detail: `no required services reporting: <full list>`.

Degraded maps to exit 2 per the existing contract, so CI can accept "pipeline warming up" while operators get a clear signal that something is off.

### D6 — Socket-proxy allowlist: add VERSION, INFO, PING, leave everything else
The healer needs:
- `GET /_ping` → Python docker SDK + CLI handshake
- `GET /version` → Python docker SDK init
- `GET /info` → healer's crash-diagnosis feature (reads `NumContainers`, `NumContainersRunning`)
- `GET /containers/json` → already allowed (`CONTAINERS=1`)
- `POST /containers/<id>/restart` → already allowed (`CONTAINERS=1 POST=1`)

All five are read-only or container-scoped. No new write surface; no images, volumes, networks, exec, swarm, secrets, build, commit. Adding `PING`, `VERSION`, `INFO` is a strict subset of what tecnativa/docker-socket-proxy explicitly documents as safe.

### D7 — Socket-proxy `read_only` + `tmpfs: /tmp`
The `read_only: true` from Batch B 3.7 is a real hardening win — the proxy binary can't be replaced at runtime. But the tecnativa image's haproxy writes its pid file to `/tmp/haproxy.pid` on startup. Combining `read_only` with no writable tmpfs means haproxy fails to start.

Fix: add `tmpfs: - /tmp:size=4m,noexec,nosuid` so the pid file can land somewhere and the rest of the filesystem stays immutable. `noexec` on /tmp is extra defence in depth against the unlikely case that an attacker can write a binary there.

### D8 — Healer healthcheck `start_period` 30s → 60s
Cold boot: the healer imports Python, opens a Docker SDK client (which blocks on the proxy handshake), runs its first health check cycle, THEN binds the HTTP server on `:8081`. On slow runners this crosses 30s. Bumping `start_period` to 60s accommodates that without affecting the steady-state interval.

### D9 — `--debug` flag prints the discovery map + raw HTTP bodies
When a probe fails, the current detail column is a one-liner like `curl failed: (28) Operation timed out`. Not enough to debug. `--debug` adds:
- A dump of `COMPOSE_MAP` at the top of the run.
- The full body of every HTTP response (indented under the probe row) so an operator can see a 404 HTML error page vs a real 503 JSON.
- The full `docker exec` stderr on compose-exec probe failures.

`--debug` implies `--no-color` and pairs with `--json` (which adds a `debug` sub-object to each check in the output).

### D10 — `healer.py` startup readiness log only (no logic changes)
Healer's boot sequence already logs. We add one line: `[healer] docker socket proxy reachable at $DOCKER_HOST` once the first successful `GET /version` lands. If the call raises, log `[healer] docker socket proxy unreachable: <err>` and retry with exponential backoff instead of exiting. This doesn't change the fix (socket-proxy allowlist) — it just gives operators a clear signal in `docker compose logs healer` when the next failure happens.

## Risks / Trade-offs

- **[Risk] `docker compose ps --format json` differs across versions.** Compose v2.5+ emits a JSON array; older v2 emits newline-delimited JSON; v1 emits an entirely different format. → Mitigation: parse with `jq --slurp` + a guard that reads `.[]` if it's an array or as individual objects. Fall back to `docker ps --filter name=zovark-` on any parse failure.
- **[Risk] `jq` parse of partial Signoz JSON on cold start.** The `/api/v1/services` endpoint can return `{"status":"success","data":null}` before the first span is indexed. → Mitigation: jq expression coerces `null` to empty array: `(.data // [])`.
- **[Risk] Adding `VERSION/INFO/PING` to socket-proxy expands attack surface.** → Accepted: all three are documented-safe read endpoints per tecnativa image docs, no write paths, no exec, no network/volume/image surface.
- **[Risk] `tmpfs /tmp` on socket-proxy masks an existing /tmp that the image already has.** → Accepted: the image's `/tmp` is empty at runtime, haproxy's pid file is the only consumer. `noexec` prevents an attacker from dropping a binary even if they find a write path.
- **[Risk] `telemetry` probe produces false alarms on slow runners during cold start.** → Mitigation: `degraded` (exit 2) is the cold-start state, `fail` (exit 1) is "no Zovark services at all". CI wiring tolerates exit 2.
- **[Risk] `telemetry` probe queries Signoz's unauthenticated `/api/v1/services` — a future Signoz version may gate it.** → Accepted: track in the runbook, refit when/if it happens.
- **[Risk] Discovering container names via `docker ps` without compose project ownership may match a foreign container.** If an operator has a separate stack with a container also named `zovark-postgres`, the probe can pick the wrong one. → Mitigation: the `ZOVARK_COMPOSE_PROJECT` env var pins the filter to `--filter "label=com.docker.compose.project=$ZOVARK_COMPOSE_PROJECT"`, defaulting to the output of `docker compose ls`. Documented in the runbook.
- **[Trade-off] `docker exec` instead of `docker compose exec`** loses the compose-network context, which some plugins use. Acceptable because the probes we run (`pg_isready`, `valkey-cli ping`, `tctl cluster health`) don't need it.

## Migration Plan

1. **Single PR, low-risk**: ship all three fixes + the new telemetry probe + the `--debug` flag + the runbook in one change. No staging needed because:
   - Script changes are read-only.
   - `docker-compose.yml` socket-proxy config is additive (three new env vars + a tmpfs).
   - Healer start_period bump is a healthcheck tuning, not a logic change.
2. **Post-merge verification**: run `scripts/stack_healthcheck.sh` against a fresh `docker compose up -d`. All eight probes (api, dashboard, signoz, telemetry, healer, redpanda, valkey, temporal, postgres) must be `pass` within 60 seconds.
3. **Rollback**: `git revert`. Zero runtime impact.

## Open Questions

- **Q1**: Should the telemetry probe also verify the span **count** is non-trivial (e.g. >10 spans in the last minute)? → **Recommendation**: no. "Service present in the list" is enough. Span-count thresholds are operator-tunable and add scope beyond this change.
- **Q2**: Should we make `telemetry` gate on metrics ingestion too (Signoz metric namespace)? → **Recommendation**: no. Traces are the primary OTEL signal; metrics are a follow-up. Track in a separate change if needed.
- **Q3**: Should `--debug` include a full-color verbose trace of every `docker exec` invocation? → **Recommendation**: only on failure. Debug output on success paths is noise.
- **Q4**: Should we add a `zvadmin diagnose` integration that shells out to this script? → **Recommendation**: defer. `zvadmin diagnose` is Go and already owns a parallel set of probes. Unifying them is a bigger architectural call than this change wants to make.
- **Q5**: Should the healer auto-retry on docker-socket-proxy unreachability forever, or exit after N attempts? → **Recommendation**: retry with exponential backoff up to 10 attempts, then log-and-exit-1 so the compose restart policy (`unless-stopped`) kicks in. 10 × 2s-backoff max = ~30 seconds of tolerance.
