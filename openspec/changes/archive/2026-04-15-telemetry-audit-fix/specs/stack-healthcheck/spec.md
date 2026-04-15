## MODIFIED Requirements

### Requirement: Skip unavailable services
The script SHALL NOT treat a missing container as a failure. If a service container is not discoverable via `docker compose ps --format json` AND is not discoverable via a `docker ps --filter "name=zovark-<alias>"` fallback, the corresponding probe SHALL be marked `skip` and SHALL NOT affect the exit code. A probe SHALL NOT emit `skip` when the container is actually running — a working-directory drift or compose project-name mismatch that causes the discovery call to return empty is a script bug, not an operator skip.

#### Scenario: Container is running but compose-json discovery misses it
- **WHEN** the operator runs `scripts/stack_healthcheck.sh` from a directory whose `docker compose ps -q postgres` returns empty, and a container named `zovark-postgres` is running on the host
- **THEN** the script discovers the container via the `docker ps --filter "name=zovark-postgres"` fallback and the `postgres` probe runs against it

#### Scenario: Container is genuinely not running
- **WHEN** `docker compose ps --format json` returns empty AND `docker ps --filter "name=zovark-postgres"` also returns empty
- **THEN** the postgres probe is marked `skip` and the overall exit code is unaffected

### Requirement: Signoz probe verifies ingest without relying on stale windows
The Signoz probe SHALL emit `pass` on a freshly-booted healthy stack without requiring operator warmup. The probe SHALL (in order) check `GET /api/v1/health`, then unless `--no-warmup` is set, issue a warmup `GET` to the URL configured by `ZOVARK_SIGNOZ_WARMUP_URL` (default `$ZOVARK_API_BASE/ready`) and sleep at least `ZOVARK_SIGNOZ_WARMUP_SLEEP_SEC` (default 3) seconds to allow the `BatchSpanProcessor` to flush, and finally query `/api/v1/services` with a 2-minute lookback window. The probe SHALL emit `pass` when the services list is non-empty, `degraded` when it is empty after a successful warmup, and `fail` when either `/api/v1/health` returns non-2xx OR the warmup URL returns non-2xx (the latter with a detail string clearly naming `warmup failure`, distinguished from `ingest empty`).

#### Scenario: Cold stack, warmup on (default)
- **WHEN** the operator runs `scripts/stack_healthcheck.sh` immediately after `docker compose up -d` on a healthy stack
- **THEN** the signoz probe issues the warmup `GET /ready`, sleeps 3 seconds, re-queries `/api/v1/services`, and emits `pass` because the warmup generated at least one span

#### Scenario: --no-warmup on cold stack
- **WHEN** the operator runs `scripts/stack_healthcheck.sh --no-warmup` immediately after `docker compose up -d`
- **THEN** the signoz probe does NOT issue the warmup request and emits `degraded` with detail `ingest empty (no services in 2-minute window)` — the legitimate observational result

#### Scenario: Warmup request fails
- **WHEN** the signoz probe issues the warmup `GET $ZOVARK_API_BASE/ready` and the response is non-2xx
- **THEN** the probe emits `fail` with detail `warmup failure: HTTP <code>` — clearly distinct from the ingest-empty detail string

#### Scenario: Warmup URL override
- **WHEN** `ZOVARK_SIGNOZ_WARMUP_URL=http://example.test/healthz` is set and the probe runs
- **THEN** the warmup request targets the override URL instead of `$ZOVARK_API_BASE/ready`

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
The script SHALL fall back to `docker ps --filter "name=zovark-<alias>"` when the compose-json discovery does not find a required service. The alias table SHALL at minimum include `postgres → zovark-postgres`, `redis → zovark-redis`, `valkey → zovark-redis`, `temporal → zovark-temporal`, `redpanda → zovark-redpanda`, `api → zovark-api`, `worker → zovark-worker-1`, `healer → zovark-healer`, `dashboard → zovark-dashboard`, `signoz → zovark-signoz`, `signoz-collector → zovark-signoz-collector`.

#### Scenario: Fallback resolves valkey via zovark-redis container
- **WHEN** compose-json discovery does not list a `redis` service, but a container named `zovark-redis` is running
- **THEN** the valkey probe resolves the container via the fallback and runs `valkey-cli ping` against it

### Requirement: Direct docker exec for container-scoped probes
The script SHALL execute container-scoped probes (valkey, temporal, postgres) via `docker exec <container-name> <cmd...>` using the container name resolved during discovery, rather than `docker compose exec <service> <cmd...>`. This removes the dependency on the compose CLI's working directory.

#### Scenario: valkey probe calls docker exec directly
- **WHEN** the valkey probe runs after a successful discovery
- **THEN** it invokes `docker exec <resolved-container> valkey-cli -a "$REDIS_PASSWORD" ping` and passes when the response contains `PONG`

### Requirement: Telemetry probe verifies specific services are reporting
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

### Requirement: --debug diagnostic flag
The script SHALL accept a `--debug` flag. When set, the script SHALL emit the discovered container map, the raw HTTP response body for every HTTP probe that fails or is degraded, and the full stderr from every compose/docker exec probe on failure. `--debug` SHALL imply `--no-color`.

#### Scenario: Debug output includes the container map
- **WHEN** the script runs with `--debug`
- **THEN** the output includes a block labelled `[debug] container map:` followed by `service = container_name` lines for every discovered service

#### Scenario: Debug output includes HTTP response bodies on failure
- **WHEN** an HTTP probe fails while `--debug` is set
- **THEN** the output includes the raw HTTP response body indented under the probe row

### Requirement: --prime flag for cold-start verification
The script SHALL accept a `--prime` flag. When set, the script SHALL invoke `scripts/e2e_probe.sh --check-fixture` before any native probes run. Failure of the `--check-fixture` step SHALL fail the entire run with exit code 1 and a detail string naming the prime-step error.

#### Scenario: --prime against a healthy stack
- **WHEN** the operator runs `scripts/stack_healthcheck.sh --prime` against a healthy stack with the fixture user seeded
- **THEN** the prime step issues a login, tickles the API, and completes successfully before the probes run, so the signoz probe emits `pass` on the first check

#### Scenario: --prime when fixture user is missing
- **WHEN** `--prime` runs against a stack where the fixture user has not been seeded
- **THEN** the script exits with status 1 and a clear error naming the prime-step failure

### Requirement: Healer probe detail includes exit code on failure
The `healer` probe SHALL include the HTTP status code (or curl exit code on connection failure) in its detail field when the probe fails, not just a generic `curl failed` message.

#### Scenario: Healer connection times out
- **WHEN** curl to the healer endpoint times out
- **THEN** the probe detail contains the curl exit code (e.g., `curl exit 28: Operation timed out`)

#### Scenario: Healer returns 503
- **WHEN** the healer `/api/health` endpoint returns HTTP 503
- **THEN** the probe detail contains `HTTP 503` and the full response body is shown in `--debug` mode

## MODIFIED Requirements

### Requirement: Structured exit codes
The script SHALL exit with status 0 when every non-skipped probe passes, status 1 when at least one probe returns `fail`, and status 2 when no probe failed but at least one was marked `degraded`. Discovery misses that fall back successfully SHALL NOT affect the exit code. A `--prime` step failure SHALL produce exit status 1 before any probes run.

#### Scenario: Telemetry degraded with everything else passing
- **WHEN** `telemetry` emits `degraded` and every other probe emits `pass`
- **THEN** the script exits with status 2

#### Scenario: --prime failure
- **WHEN** `--prime` is set and the prime step fails
- **THEN** the script exits with status 1 without running the native probes
