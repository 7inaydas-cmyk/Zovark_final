## 1. Container discovery

- [ ] 1.1 Add `_discover_compose_containers()` in `scripts/stack_healthcheck.sh` that runs `docker compose ps --format json` once and parses both JSON-array and newline-delimited-JSON formats via `jq --slurp '.[] | . | if type == "array" then .[] else . end'`
- [ ] 1.2 Populate an associative array `COMPOSE_MAP` (declare -A) mapping compose service name → container name from the json output
- [ ] 1.3 Add `_fallback_container_alias <service>` table with: `postgres→zovark-postgres`, `redis→zovark-redis`, `valkey→zovark-redis`, `temporal→zovark-temporal`, `redpanda→zovark-redpanda`, `api→zovark-api`, `worker→zovark-worker-1`, `healer→zovark-healer`, `dashboard→zovark-dashboard`, `signoz→zovark-signoz`, `signoz-collector→zovark-signoz-collector`
- [ ] 1.4 Add `_compose_container_name <service>` that first reads `COMPOSE_MAP[<service>]`, then falls back to `docker ps --filter "name=$(_fallback_container_alias <service>)" --format '{{.Names}}' | head -1`, and prints the resolved container name (or empty)
- [ ] 1.5 Call `_discover_compose_containers` at the top of the script (after jq-presence check, before probes run) so the sweep happens exactly once per invocation

## 2. Use direct docker exec for compose-scoped probes

- [ ] 2.1 Rewrite `_compose_exec_check` to resolve the container name via `_compose_container_name` and invoke `docker exec -i <container-name> <cmd...>` directly
- [ ] 2.2 Update `check_valkey` to resolve via `_compose_container_name redis` and run `valkey-cli -a "$REDIS_PASSWORD" ping`
- [ ] 2.3 Update `check_temporal` to resolve via `_compose_container_name temporal` and run `tctl --address temporal:7233 cluster health`
- [ ] 2.4 Update `check_postgres` to resolve via `_compose_container_name postgres` and run `pg_isready -U zovark -d zovark`
- [ ] 2.5 When a container name cannot be resolved at all, mark the probe `skip` with detail `container '<service>' not running`
- [ ] 2.6 Ensure the `PONG` / `SERVING` / `accepting connections` post-check still runs on the new exec path

## 3. Telemetry probe

- [ ] 3.1 Append `telemetry` to the `CHECKS` array (after `signoz`, before `healer`)
- [ ] 3.2 Implement `check_telemetry` that queries `$ZOVARK_SIGNOZ_BASE/api/v1/services?start=<now-10m>&end=<now>` and parses via jq `[.data[]?.serviceName, .services[]?.serviceName, .[]?.serviceName] | unique | map(select(. != null))` to survive Signoz response-shape drift
- [ ] 3.3 Read `ZOVARK_TELEMETRY_REQUIRED_SERVICES` (default `zovark-api,zovark-worker`), split on commas, compare against discovered services
- [ ] 3.4 Emit `pass` when every required service is present, `degraded` when at least one but not all are present (detail: `missing: <csv>`), `fail` when none are present (detail: `no required services reporting: <csv>`)
- [ ] 3.5 Emit `fail` with the upstream status code in the detail when the Signoz services API returns non-2xx
- [ ] 3.6 Add `telemetry` to the `--help` usage text under the "valid probe names" list

## 4. --debug flag and diagnostic output

- [ ] 4.1 Add `--debug` to the CLI argument parser; set `DEBUG=1` and imply `NO_COLOR=1`
- [ ] 4.2 When `DEBUG=1`, emit `[debug] container map:` with one line per `COMPOSE_MAP` entry at the top of the run
- [ ] 4.3 Capture HTTP response bodies in `_http_check` and emit them indented under the probe row when `DEBUG=1` and the probe status is `fail` or `degraded`
- [ ] 4.4 In `_compose_exec_check`, capture the stderr of `docker exec` and emit it on failure under `DEBUG=1`
- [ ] 4.5 Update `check_healer` so its `fail` detail string contains the curl exit code (e.g., `curl exit 28: Operation timed out`) or the HTTP status code when the request reached the server
- [ ] 4.6 Update usage text / `--help` to document `--debug`

## 5. Compose file fixes for the healer regression

- [ ] 5.1 Add `VERSION: 1`, `INFO: 1`, `PING: 1` to the `docker-socket-proxy` service `environment:` block in `docker-compose.yml`
- [ ] 5.2 Add `tmpfs: ["/tmp:size=4m,noexec,nosuid"]` to the `docker-socket-proxy` service so haproxy's pid file has a writable location under `read_only: true`
- [ ] 5.3 Bump the `healer` service `healthcheck.start_period` from `30s` to `60s` in `docker-compose.yml`

## 6. Healer startup readiness log

- [ ] 6.1 In `agent/healer.py`, after the existing startup logging and before the first health-check loop iteration, add a one-shot probe that calls `http://$DOCKER_HOST/version` via the Python docker SDK and logs `[healer] docker socket proxy reachable at <addr>` on success
- [ ] 6.2 On failure, log `[healer] docker socket proxy unreachable: <err>` and retry with exponential backoff (2s → 4s → 8s → 16s, capped at 10 attempts, ~30s total)
- [ ] 6.3 If all retries fail, `sys.exit(1)` so the compose `restart: unless-stopped` policy recycles the container
- [ ] 6.4 Confirm the healer's existing HTTP server on `:8081` binds **before** the Docker SDK init runs so its `/api/health` endpoint is reachable even while the docker socket probe is still retrying

## 7. Docs

- [ ] 7.1 Update `CLAUDE.md` "How to Run" section: note that `telemetry` is the canonical "is the pipeline instrumented?" probe
- [ ] 7.2 Create `docs/RUNBOOK_HEALTHCHECK.md` with three sections: "stack_healthcheck reports skip for postgres/temporal/valkey", "healer probe times out after upgrade", "telemetry probe reports degraded" — each with copy-paste diagnostic commands

## 8. Verification

- [ ] 8.1 `bash -n scripts/stack_healthcheck.sh` exits 0 after all edits
- [ ] 8.2 `scripts/stack_healthcheck.sh --help` lists `telemetry` in the "valid probe names" section
- [ ] 8.3 `scripts/stack_healthcheck.sh --help` includes the `--debug` flag in the OPTIONS section
- [ ] 8.4 `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"` succeeds after compose edits
- [ ] 8.5 `scripts/stack_healthcheck.sh --json --skip api,dashboard,signoz,telemetry,healer,redpanda,valkey,temporal,postgres` exits 0 with an empty-ish JSON checks array (smoke test that all new code paths parse)
- [ ] 8.6 `ZOVARK_SIGNOZ_BASE=http://127.0.0.1:1 scripts/stack_healthcheck.sh --skip api,dashboard,signoz,healer,redpanda,valkey,temporal,postgres` exits 1 with `telemetry` failing and the detail containing the upstream error
- [ ] 8.7 `ZOVARK_TELEMETRY_REQUIRED_SERVICES=nonexistent-service scripts/stack_healthcheck.sh --skip api,dashboard,signoz,healer,redpanda,valkey,temporal,postgres` against a live Signoz instance exits 1 with `telemetry` fail detail listing the required service name
- [ ] 8.8 Manual dry run against the live compose stack: every probe including `telemetry` must reach `pass` within 60 seconds of a fresh `docker compose up -d`
- [ ] 8.9 Confirm `docker compose logs healer` shows the `docker socket proxy reachable at …` line after stack_healthcheck flips healer to `pass`
