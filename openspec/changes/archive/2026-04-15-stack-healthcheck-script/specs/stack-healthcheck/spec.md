## ADDED Requirements

### Requirement: Single-command stack probe
The repository SHALL ship an executable script `scripts/stack_healthcheck.sh` that, when invoked without arguments, probes the running Zovark stack and prints a pass/fail table. The script SHALL be committed with mode 0755.

#### Scenario: Operator runs the script on a healthy stack
- **WHEN** every Compose service in the core profile is running and healthy
- **THEN** `scripts/stack_healthcheck.sh` exits with status 0 and the final line reads `OVERALL: pass`

#### Scenario: Executable bit is set
- **WHEN** a developer clones the repo and runs `test -x scripts/stack_healthcheck.sh`
- **THEN** the command exits with status 0

### Requirement: Required probes
The script SHALL probe the following eight components, in this order: API, Dashboard, Signoz, Healer, Redpanda, Valkey, Temporal, PostgreSQL. Each probe SHALL have a per-check timeout no longer than five seconds.

#### Scenario: API probe uses the /ready endpoint
- **WHEN** the API probe runs
- **THEN** it issues `curl -fsS --max-time 5 $ZOVARK_API_BASE/ready` and passes only when the response body contains `"status":"ready"`

#### Scenario: Dashboard probe uses the /health endpoint
- **WHEN** the Dashboard probe runs
- **THEN** it issues `curl -fsS --max-time 5 $ZOVARK_DASHBOARD_BASE/health` and passes only when the HTTP status is 200

#### Scenario: Signoz probe verifies both health and trace ingestion
- **WHEN** the Signoz probe runs
- **THEN** it first issues `curl -fsS --max-time 5 $ZOVARK_SIGNOZ_BASE/api/v1/health`, which must return 200, and then issues a request to the services API with a ten-minute lookback window and asserts the response is a non-empty JSON array

#### Scenario: Signoz trace ingestion is empty
- **WHEN** the Signoz /api/v1/health endpoint returns 200 but the services API returns an empty list
- **THEN** the probe emits `degraded` with a detail string explaining trace ingestion is idle, and the overall exit code becomes 2 unless some other check failed

#### Scenario: Healer probe uses its own health endpoint
- **WHEN** the Healer probe runs
- **THEN** it issues `curl -fsS --max-time 5 $ZOVARK_HEALER_BASE/api/health` and passes only when the HTTP status is 200

#### Scenario: Redpanda probe is a TCP connect
- **WHEN** the Redpanda probe runs
- **THEN** it opens a TCP connection to `127.0.0.1:19092` via `bash -c '</dev/tcp/127.0.0.1/19092'` wrapped in a `timeout 5` guard, and passes only when the connect succeeds

#### Scenario: Valkey probe uses valkey-cli ping
- **WHEN** the Valkey probe runs
- **THEN** it invokes `docker compose exec -T redis valkey-cli -a "$REDIS_PASSWORD" ping` and passes only when the output contains `PONG`

#### Scenario: Temporal probe uses tctl cluster health
- **WHEN** the Temporal probe runs
- **THEN** it invokes `docker compose exec -T temporal tctl --address temporal:7233 cluster health` and passes when the output contains `SERVING` or the exit status is 0

#### Scenario: PostgreSQL probe uses pg_isready
- **WHEN** the PostgreSQL probe runs
- **THEN** it invokes `docker compose exec -T postgres pg_isready -U zovark -d zovark` and passes only when the exit status is 0

### Requirement: Skip unavailable services
The script SHALL NOT treat a missing container as a failure. If a service container is not listed by `docker compose ps`, the corresponding probe SHALL be marked `skip` and SHALL NOT affect the exit code.

#### Scenario: Postgres container is intentionally not started
- **WHEN** `docker compose ps -q postgres` returns empty
- **THEN** the Postgres probe is marked `skip` and the overall result is not affected by it

### Requirement: Skip list from the command line
The script SHALL accept a `--skip <list>` flag whose argument is a comma-separated list of probe names. Listed probes SHALL NOT be executed and SHALL be marked `skip` in the output.

#### Scenario: Skip signoz and redpanda
- **WHEN** the operator runs `scripts/stack_healthcheck.sh --skip signoz,redpanda`
- **THEN** the Signoz and Redpanda probes are not executed and are marked `skip` in the table, and the overall exit code reflects only the remaining probes

### Requirement: Colored table output by default
When stdout is attached to a TTY, the script SHALL emit ANSI colors: green for `pass`, red for `fail`, yellow for `degraded`, gray for `skip`. When stdout is not a TTY, or when `--no-color` is passed, the script SHALL emit plain text without any ANSI escape sequences.

#### Scenario: Colors are emitted on a TTY
- **WHEN** the script is run interactively on a terminal
- **THEN** the output contains ANSI escape sequences for color codes

#### Scenario: Colors are suppressed with --no-color
- **WHEN** the script is run with `--no-color`
- **THEN** the output contains no ANSI escape sequences regardless of TTY detection

### Requirement: Machine-readable JSON output
The script SHALL accept a `--json` flag. When set, it SHALL suppress all human-readable output and emit a single JSON object on stdout of the form `{"schema_version": 1, "overall": "pass"|"fail"|"degraded", "checks": [{"name": ..., "status": ..., "latency_ms": ..., "detail": ...}, ...]}`. `--json` SHALL imply `--no-color`.

#### Scenario: JSON output is valid
- **WHEN** the script runs with `--json` and every probe passes
- **THEN** the output is a single JSON object with `overall == "pass"`, a `checks` array of length equal to the number of non-skipped probes, and each check has `name`, `status`, `latency_ms`, and `detail` fields

#### Scenario: Schema version is present
- **WHEN** the script runs with `--json`
- **THEN** the output contains `"schema_version": 1` as a top-level key

### Requirement: Structured exit codes
The script SHALL exit with status 0 when every non-skipped probe passes, status 1 when at least one probe fails, and status 2 when no probe failed but at least one was marked `degraded`.

#### Scenario: All pass
- **WHEN** every non-skipped probe returns `pass`
- **THEN** the script exits with status 0

#### Scenario: At least one hard failure
- **WHEN** any probe returns `fail`
- **THEN** the script exits with status 1, regardless of whether other probes were degraded

#### Scenario: Only degraded, no failures
- **WHEN** no probe returns `fail` and at least one returns `degraded`
- **THEN** the script exits with status 2

### Requirement: Environment variable overrides
The script SHALL honor the following environment variables, falling back to compose-local defaults when unset: `ZOVARK_API_BASE` (default `http://127.0.0.1:8090`), `ZOVARK_DASHBOARD_BASE` (default `http://127.0.0.1:3000`), `ZOVARK_SIGNOZ_BASE` (default `http://127.0.0.1:3301`), `ZOVARK_HEALER_BASE` (default `http://127.0.0.1:8081`), `REDIS_PASSWORD` (required for Valkey probe).

#### Scenario: API base URL override
- **WHEN** the script is run with `ZOVARK_API_BASE=http://remote-host:8090 scripts/stack_healthcheck.sh`
- **THEN** the API probe targets `http://remote-host:8090/ready` instead of the loopback default

#### Scenario: Valkey probe aborts when password is missing
- **WHEN** `REDIS_PASSWORD` is unset and the Valkey probe runs
- **THEN** the probe emits `fail` with a detail string explaining the missing variable, rather than passing with an unauthenticated PING

### Requirement: Hard dependency on jq
The script SHALL fail fast with exit status 2 and a clear error message if `jq` is not on the PATH. The error message SHALL include an install hint.

#### Scenario: jq is missing
- **WHEN** the script runs on a host where `command -v jq` returns empty
- **THEN** the script exits with status 2 and prints an error line to stderr that contains the substring `jq` and an install hint such as `apt install jq` or `brew install jq`

### Requirement: Read-only safety
The script SHALL NOT write to any file, restart any container, or mutate any state. It SHALL be safe to run repeatedly from any account with Docker access.

#### Scenario: Running the script ten times in a row leaves state unchanged
- **WHEN** the script is run ten times in succession against a healthy stack
- **THEN** no container is restarted, no file is created or modified outside of stdout/stderr, and the tenth run produces the same overall exit code as the first
