## 1. Script scaffold

- [x] 1.1 Create `scripts/stack_healthcheck.sh` with shebang `#!/usr/bin/env bash`, `set -euo pipefail`, and a header comment explaining purpose, exit codes, and env var contract
- [x] 1.2 Add `chmod +x scripts/stack_healthcheck.sh` step so the committed file has mode 0755
- [x] 1.3 Add jq-presence guard at the top of the script that exits 2 with a clear install hint if `command -v jq` is empty
- [x] 1.4 Define env var defaults: `ZOVARK_API_BASE`, `ZOVARK_DASHBOARD_BASE`, `ZOVARK_SIGNOZ_BASE`, `ZOVARK_HEALER_BASE`, `TIMEOUT`, `REDIS_PASSWORD`

## 2. CLI argument parsing

- [x] 2.1 Parse `--json`, `--no-color`, `--skip <list>`, `--timeout <seconds>`, `--help` flags via a simple `case` loop
- [x] 2.2 `--json` implies `--no-color`
- [x] 2.3 `--help` prints usage and exits 0; usage text documents every flag and env var

## 3. Probe primitives

- [x] 3.1 Write `emit <status> <latency_ms> <detail>` helper that stores the current check's result in `CHECK_STATUS`, `CHECK_LATENCY_MS`, `CHECK_DETAIL` for the runner to collect
- [x] 3.2 Write `_now_ms` helper that returns milliseconds since epoch (portable: use `date +%s%3N` where supported, fallback to `python3 -c 'import time; print(int(time.time()*1000))'` gated on Python availability, else `date +%s` × 1000)
- [x] 3.3 Write `_http_check <name> <url> <success_jq_expr>` helper that does `curl -fsS --max-time $TIMEOUT`, measures latency, and runs a jq expression against the body
- [x] 3.4 Write `_tcp_check <name> <host> <port>` helper using `timeout $TIMEOUT bash -c '</dev/tcp/$host/$port'`
- [x] 3.5 Write `_compose_exec_check <name> <service> <command...>` helper that first checks `docker compose ps -q <service>` is non-empty, marks `skip` if missing, else runs the command and passes on exit 0

## 4. Individual probes

- [x] 4.1 `check_api` — call `_http_check api "$ZOVARK_API_BASE/ready" '.status == "ready"'`
- [x] 4.2 `check_dashboard` — call `_http_check dashboard "$ZOVARK_DASHBOARD_BASE/health" '.'` (body must parse as anything — the endpoint returns plain `ok`, so fall back to a raw 200 check)
- [x] 4.3 `check_signoz` — call `/api/v1/health` for a 200, then `/api/v1/services?start=<now-10m>&end=<now>` and assert the response is a JSON array of length ≥ 1; emit `degraded` (not `fail`) when the array is empty
- [x] 4.4 `check_healer` — call `_http_check healer "$ZOVARK_HEALER_BASE/api/health" '.'` (any 200 is pass)
- [x] 4.5 `check_redpanda` — call `_tcp_check redpanda 127.0.0.1 19092`
- [x] 4.6 `check_valkey` — `_compose_exec_check valkey redis valkey-cli -a "$REDIS_PASSWORD" ping`; fail fast with detail if `REDIS_PASSWORD` is unset
- [x] 4.7 `check_temporal` — `_compose_exec_check temporal temporal tctl --address temporal:7233 cluster health`; grep output for `SERVING`
- [x] 4.8 `check_postgres` — `_compose_exec_check postgres postgres pg_isready -U zovark -d zovark`

## 5. Runner and output

- [x] 5.1 Implement the runner that iterates over `CHECKS=(api dashboard signoz healer redpanda valkey temporal postgres)`, skipping entries in `--skip`, calling `check_<name>` for each, and appending `(name, status, latency, detail)` to a `RESULTS` array
- [x] 5.2 Implement colored table renderer: fixed-width columns (name, status, latency, detail), coloring via `printf` and ANSI escapes; respect `--no-color` and TTY detection (`[ -t 1 ]`)
- [x] 5.3 Implement JSON renderer that emits `{"schema_version":1,"overall":...,"checks":[...]}` via `jq -n --argjson`
- [x] 5.4 Compute `OVERALL` from the `RESULTS` array: `fail` if any status is `fail`; else `degraded` if any is `degraded`; else `pass`
- [x] 5.5 Print `OVERALL: pass|degraded|fail` as the final line in table mode (bold/colored); include it as `overall` in JSON mode
- [x] 5.6 Exit with the appropriate code: 0 pass, 1 fail, 2 degraded (skips never change the exit code)

## 6. Integration and docs

- [x] 6.1 Update `CLAUDE.md` "How to Run" section with a one-line pointer: `scripts/stack_healthcheck.sh` as the first command after `docker compose up -d`
- [x] 6.2 Add a step to `.github/workflows/ci.yml` that runs `scripts/stack_healthcheck.sh --json` after the existing `docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build` step and gates the integration suite on exit 0
- [x] 6.3 Document the exit-code contract and skip/include behavior in the script's header comment

## 7. Verification

- [x] 7.1 `bash -n scripts/stack_healthcheck.sh` exits 0 (syntax check)
- [x] 7.2 `scripts/stack_healthcheck.sh --help` prints usage and exits 0
- [x] 7.3 `scripts/stack_healthcheck.sh --json --skip signoz,redpanda,valkey,temporal,postgres,healer,dashboard` (only API probe active) against a running API produces a valid JSON payload with `schema_version == 1` and `overall == "pass"`
- [x] 7.4 Running the script with an unreachable API base returns `overall == "fail"` and exit code 1
- [x] 7.5 Running the script with `--skip` targeting every check produces exit 0 and an empty-ish JSON checks array
- [x] 7.6 Confirm `test -x scripts/stack_healthcheck.sh` is true in the committed tree
- [x] 7.7 Manual smoke test: run against the local compose stack (when available) and verify the colored table renders correctly
