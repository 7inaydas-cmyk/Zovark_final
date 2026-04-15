## Why

First live run of `scripts/stack_healthcheck.sh` after Batch B exposed three real problems that a static audit couldn't catch:

1. **valkey / temporal / postgres all report `skip`** — even though the containers are up. The script's `_compose_present` helper calls `docker compose ps -q <service>` which is fragile: it depends on the working directory containing the compose file, the compose project name matching `zovark`, and the service-name key (`postgres`, `redis`, `temporal`) being what `docker compose` knows about. When any of those drift, every `_compose_exec_check` probe silently marks itself `skip` instead of `fail`, which is the worst of both worlds.

2. **healer probe is timing out** — Batch B routed healer's Docker calls through `docker-socket-proxy` via `DOCKER_HOST=tcp://docker-socket-proxy:2375` and dropped the direct `/var/run/docker.sock` bind mount. That tightened security but also disabled several Docker API endpoints the healer needs (`GET /version`, `GET /info`, `GET /_ping`) because `tecnativa/docker-socket-proxy` allowlist was left at `CONTAINERS=1 POST=1` only. On top of that, the proxy itself was marked `read_only: true` in 3.7 which breaks its internal haproxy (needs `/tmp` writable to stash a pid file). The combined effect: healer starts, tries to init its Docker client, fails, logs, and the compose healthcheck on `:8081/api/health` never sees a 200 because the probe was waiting on the Docker client init.

3. **Signoz healthcheck only verifies Signoz is alive, not that the Zovark pipeline is actually emitting traces** — the existing `check_signoz` probe hits `/api/v1/health` and `/api/v1/services` but only asserts "at least one service reported in the last 10 minutes". That passes even when Zovark itself is dark and only the Signoz collector's self-instrumentation is showing up. The only honest way to verify the pipeline is instrumented end-to-end is to assert specific service names: `zovark-api` and `zovark-worker` must appear in the recent-services list.

## What Changes

- **Rewrite `_compose_present` and add `_compose_container_name`** in `scripts/stack_healthcheck.sh`:
  - Run `docker compose ps --format json` once at script start, cache the result, and build a `service → container_name` map via `jq`. Handle both older (`newline-delimited JSON objects`) and newer (`JSON array`) compose output formats.
  - `_compose_exec_check` now uses `docker exec <container_name>` directly instead of `docker compose exec <service>`, so it works regardless of `COMPOSE_PROJECT_NAME` / working directory / compose CLI version.
  - When a service still can't be found after the json sweep, fall back to `docker ps --filter "name=zovark-<service>"` with a known alias table (`valkey` → `zovark-redis`, `postgres` → `zovark-postgres`, `temporal` → `zovark-temporal`). Only if **that** also fails does the probe emit `skip`.

- **Add a new `telemetry` probe** at the end of `CHECKS`:
  - Probes `$ZOVARK_SIGNOZ_BASE/api/v1/services?start=<now-10m>&end=<now>`, parses the response with `jq`, and asserts `zovark-api` and `zovark-worker` are both present.
  - Returns `pass` when both are present, `degraded` when only one is (warm-up / one-sided outage), `fail` when neither is (pipeline is dark).
  - Required services are overridable via `ZOVARK_TELEMETRY_REQUIRED_SERVICES=zovark-api,zovark-worker` so air-gap deployments that rename services don't break the probe.
  - The existing `signoz` probe is unchanged — it still verifies Signoz itself is healthy. `telemetry` is a separate probe that verifies the pipeline is emitting into it.

- **Unblock the healer by fixing the socket proxy configuration**:
  - Add `PING: 1`, `VERSION: 1`, `INFO: 1` to `docker-socket-proxy` environment so the Docker SDK init probe (`GET /version`) and the healer's periodic crash-diagnosis (`GET /info` + `GET /containers/json`) actually work. `CONTAINERS` and `POST` stay on; the more dangerous surfaces (`EXEC`, `VOLUMES`, `NETWORKS`, `IMAGES`, `SWARM`, `SECRETS`, `BUILD`, `COMMIT`) stay off.
  - Add `tmpfs: - /tmp` to `docker-socket-proxy` so the `read_only: true` layer doesn't break haproxy's internal pid / stats file.
  - Bump healer's healthcheck `start_period` from 30s to 60s so slow Docker SDK init on first boot doesn't trip the probe.
  - Add a startup ping from the healer to `tcp://docker-socket-proxy:2375` that fails closed with a clear error message in the container logs if the proxy isn't reachable. Operators see "healer waiting for docker-socket-proxy" instead of a silent timeout.

- **Diagnostic flags added to `stack_healthcheck.sh`**:
  - `--debug` prints the discovered container map and the raw response body for every HTTP probe, to shorten the "why is this failing?" loop on live stacks.
  - The `healer` probe's detail field now includes the exit code and a 120-char head of stderr on failure (previously just "curl failed").
  - The new `telemetry` probe's detail field names the missing services explicitly (e.g., `missing: zovark-worker`).

- **Documentation**:
  - `CLAUDE.md` "Contributor Quickstart" / "How to Run" sections point at the new `telemetry` probe as the canonical "is the pipeline instrumented?" check.
  - A new `docs/RUNBOOK_HEALTHCHECK.md` captures the three failure modes the live run just exposed, with copy-paste diagnostic commands for each.

## Capabilities

### New Capabilities

- (none — this change extends the existing `stack-healthcheck` capability)

### Modified Capabilities

- `stack-healthcheck`: Adds requirements for dynamic container-name discovery, a dedicated telemetry probe, and the `--debug` diagnostic flag. Tightens the exit-code contract so a dynamic discovery failure on a required service produces `fail`, not `skip`.

## Impact

- **Affected code**: `scripts/stack_healthcheck.sh` (rewrite of `_compose_present`, add `_discover_compose_containers` / `_compose_container_name`, add `check_telemetry`, add `--debug`), `docker-compose.yml` (socket-proxy env + tmpfs + healer start_period), `agent/healer.py` (optional startup readiness log), `CLAUDE.md`, new `docs/RUNBOOK_HEALTHCHECK.md`.
- **Runtime impact**: none to core services. Socket-proxy gains `/tmp` tmpfs and three new allowlisted endpoints (`VERSION`, `INFO`, `PING`). Healer's healthcheck is more tolerant on cold boot.
- **Dependencies**: `jq` (already required by `smoke_test_100.sh` and the existing `stack_healthcheck.sh`). No new packages.
- **Risk**: low. The socket-proxy allowlist additions are strictly read-only (`VERSION`/`INFO`/`PING`) — no new write surface. The container-name discovery change is additive with a fallback to the existing behavior. The telemetry probe is a new check that can be skipped via `--skip telemetry` if Signoz isn't in the profile.
- **Breaking**: none. `--skip signoz,telemetry` gives the previous behavior exactly, and the new container discovery degrades gracefully when `docker compose ps --format json` is unavailable (older compose CLI).
