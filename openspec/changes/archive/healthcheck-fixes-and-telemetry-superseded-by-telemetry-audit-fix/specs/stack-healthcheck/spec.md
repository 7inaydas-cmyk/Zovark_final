## MODIFIED Requirements

### Requirement: Skip unavailable services
The script SHALL NOT treat a missing container as a failure. If a service container is not discoverable via `docker compose ps --format json` AND is not discoverable via a `docker ps --filter "name=zovark-<alias>"` fallback, the corresponding probe SHALL be marked `skip` and SHALL NOT affect the exit code. A probe SHALL NOT emit `skip` when the container is actually running — a working-directory drift or compose project-name mismatch that causes the discovery call to return empty is a script bug, not an operator skip.

#### Scenario: Container is running but discovery misses it via compose json
- **WHEN** the operator runs `scripts/stack_healthcheck.sh` from a directory whose `docker compose ps -q postgres` returns empty, and a container named `zovark-postgres` is running on the host
- **THEN** the script discovers the container via the `docker ps --filter "name=zovark-postgres"` fallback and the `postgres` probe runs against it

#### Scenario: Container is genuinely not running
- **WHEN** `docker compose ps --format json` returns empty AND `docker ps --filter "name=zovark-postgres"` also returns empty
- **THEN** the postgres probe is marked `skip` and the overall exit code is unaffected

## ADDED Requirements

### Requirement: Dynamic container-name discovery
The script SHALL discover container names dynamically at the start of every run by parsing `docker compose ps --format json` and building a `service → container_name` map. The map SHALL be cached for the lifetime of the run. Every `_compose_exec_check` probe SHALL look up its target container via the map rather than calling `docker compose ps -q <service>` per-probe.

#### Scenario: Discovery sweep runs exactly once per invocation
- **WHEN** the script runs with every probe active
- **THEN** `docker compose ps --format json` is invoked at most once, and subsequent probe lookups read from the cached map

#### Scenario: Discovery handles both JSON array and newline-delimited JSON formats
- **WHEN** the installed `docker compose` version emits either a top-level JSON array or newline-delimited JSON objects
- **THEN** the discovery sweep correctly populates the map in both cases

### Requirement: Container-name fallback via docker ps filter
The script SHALL fall back to `docker ps --filter "name=zovark-<alias>" --format '{{.Names}}'` when the compose-json discovery does not find a required service. The alias table SHALL at minimum include `postgres → zovark-postgres`, `redis → zovark-redis`, `valkey → zovark-redis`, `temporal → zovark-temporal`, `redpanda → zovark-redpanda`, `api → zovark-api`, `worker → zovark-worker-1`, `healer → zovark-healer`, `dashboard → zovark-dashboard`, `signoz → zovark-signoz`, `signoz-collector → zovark-signoz-collector`.

#### Scenario: Fallback resolves valkey via zovark-redis container
- **WHEN** compose-json discovery does not list a `redis` service, but a container named `zovark-redis` is running
- **THEN** the valkey probe resolves the container via the fallback and runs `valkey-cli ping` against it

### Requirement: Direct docker exec for container-scoped probes
The script SHALL execute container-scoped probes (valkey, temporal, postgres) via `docker exec <container-name> <cmd...>` using the container name resolved during discovery, rather than `docker compose exec <service> <cmd...>`. This removes the dependency on the compose CLI's working directory.

#### Scenario: valkey probe calls docker exec directly
- **WHEN** the valkey probe runs after a successful discovery
- **THEN** it invokes `docker exec zovark-redis valkey-cli -a "$REDIS_PASSWORD" ping` (not `docker compose exec redis ...`) and passes when the response contains `PONG`

### Requirement: Telemetry probe verifies end-to-end trace flow
The script SHALL include a probe named `telemetry` that queries `$ZOVARK_SIGNOZ_BASE/api/v1/services?start=<now-10m>&end=<now>` and asserts the response contains a specific list of required service names (default `zovark-api,zovark-worker`, override via `ZOVARK_TELEMETRY_REQUIRED_SERVICES`).

#### Scenario: Both required services are present
- **WHEN** the Signoz services API returns a list containing both `zovark-api` and `zovark-worker`
- **THEN** the telemetry probe emits `pass`

#### Scenario: One required service is missing
- **WHEN** the Signoz services API returns a list containing `zovark-api` but not `zovark-worker`
- **THEN** the telemetry probe emits `degraded` with a detail string containing `missing: zovark-worker`, and the overall exit code is 2 unless another probe failed

#### Scenario: No required services are present
- **WHEN** the Signoz services API returns an empty list or a list containing only foreign services
- **THEN** the telemetry probe emits `fail` with a detail string listing the required services, and the overall exit code is 1

#### Scenario: Signoz unavailable
- **WHEN** the Signoz services API returns a non-2xx response
- **THEN** the telemetry probe emits `fail` with a detail string indicating the upstream error, and the overall exit code is 1

#### Scenario: Required services overridable via env
- **WHEN** the operator sets `ZOVARK_TELEMETRY_REQUIRED_SERVICES=zovark-worker` and runs the script
- **THEN** the telemetry probe asserts only `zovark-worker` and passes if `zovark-api` is absent

### Requirement: --debug diagnostic flag
The script SHALL accept a `--debug` flag. When set, the script SHALL emit the discovered container map, the raw HTTP response body for every HTTP probe, and the full stderr from every compose/docker exec probe on failure. `--debug` SHALL imply `--no-color`.

#### Scenario: Debug output includes the container map
- **WHEN** the script runs with `--debug`
- **THEN** the output includes a block labelled `[debug] container map:` followed by `service = container_name` lines for every discovered service

#### Scenario: Debug output includes HTTP response bodies on failure
- **WHEN** an HTTP probe fails while `--debug` is set
- **THEN** the output includes the raw HTTP response body indented under the probe row

### Requirement: Healer probe detail includes exit code on failure
The `healer` probe SHALL include the HTTP status code (or curl exit code on connection failure) in its detail field when the probe fails, not just a generic `curl failed` message.

#### Scenario: Healer returns 503
- **WHEN** the healer `/api/health` endpoint returns HTTP 503
- **THEN** the probe detail contains `HTTP 503` and the full response body is shown in `--debug` mode

#### Scenario: Healer connection times out
- **WHEN** curl to the healer endpoint times out
- **THEN** the probe detail contains the curl exit code (e.g., `curl exit 28: Operation timed out`)

## MODIFIED Requirements

### Requirement: Signoz probe verifies both health and trace ingestion
When the Signoz probe runs, it SHALL first issue `curl -fsS --max-time 5 $ZOVARK_SIGNOZ_BASE/api/v1/health`, which must return 200, and then issue a request to the services API with a ten-minute lookback window and assert the response is a non-empty JSON array. An empty services list SHALL emit `degraded` (not `fail`) because trace ingestion may be in a cold-start window. Verification that specific Zovark services are reporting traces SHALL be the responsibility of the separate `telemetry` probe.

#### Scenario: Signoz is healthy and at least one service is reporting
- **WHEN** `/api/v1/health` returns 200 and `/api/v1/services` returns a non-empty list
- **THEN** the probe emits `pass` regardless of whether specific Zovark services are present

#### Scenario: Signoz is healthy but no services are reporting yet
- **WHEN** `/api/v1/health` returns 200 but `/api/v1/services` returns an empty list
- **THEN** the probe emits `degraded` with a detail string explaining trace ingestion is idle

### Requirement: Structured exit codes
The script SHALL exit with status 0 when every non-skipped probe passes, status 1 when at least one probe returns `fail`, and status 2 when no probe failed but at least one was marked `degraded`. Discovery misses that fall back successfully SHALL NOT affect the exit code.

#### Scenario: Telemetry degraded with everything else passing
- **WHEN** `telemetry` emits `degraded` and every other probe emits `pass`
- **THEN** the script exits with status 2
