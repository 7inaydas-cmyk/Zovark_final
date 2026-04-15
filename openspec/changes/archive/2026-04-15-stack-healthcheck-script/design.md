## Context

The Zovark stack is 10+ containers depending on the compose profile. Today there are three overlapping "is it up?" affordances:

1. `GET :8090/ready` — Go API only; checks PG + Redis + Temporal (its own dependencies) via pgx ping, Redis PING, and Temporal client connectivity.
2. `GET :8090/health` — legacy Go health handler; the audit-round-2-fixes change set it up with a 3-second `http.Client` but it still always returns 200 because the `/health` vs `/live` split is behind a feature flag (`ZOVARK_HARDENING_HEALTH_503`, not active yet).
3. `agent/healer.py` — runs continuously inside `zovark-healer` container, has its own polling. Not invokable ad-hoc from an operator terminal.

Gap: there is no single command an operator or CI job can run after `docker compose up -d` that answers "is EVERY moving part actually up right now?". After the port-binding changes in audit-round-2-fixes 3.9, most services only listen on `127.0.0.1:`, so `curl` commands scattered across a runbook are more likely to be wrong. We want one script that knows the canonical probe for each service, respects env overrides, prints a table a human can read in 2 seconds, and can also emit JSON for CI.

Constraints:
- **Air-gap deployments**: no outbound network, no package install at runtime. The script must work with whatever is in the base image of the operator's host (bash, curl, jq, nc, docker CLI). No Python.
- **MinGW/Git-Bash on Windows**: some operators run this from Windows. We need `MSYS_NO_PATHCONV=1` where needed and avoid Linux-only flags on `nc`.
- **Exit code contract**: CI and the healer should be able to tell apart "everything OK" (0) from "critical component down" (1) from "optional degraded" (2) without parsing text.
- **Partial profiles**: a dev-tier compose may have `monitoring` / `tracing` / `siem-lab` profiles switched off. Script must skip cleanly instead of reporting false failures.

Stakeholders: SOC platform engineering (CI), operations (runbook), healer (may exec the script periodically in future), on-call (paging).

## Goals / Non-Goals

**Goals:**
- **One command, full coverage**: probe API, dashboard, Signoz, healer, Redpanda, Valkey, Temporal, Postgres in a single invocation.
- **Honest Signoz check**: not just "TCP port open" — actually verify the `/api/v1/health` endpoint AND that trace ingestion is live by confirming at least one service has reported a span in the last 10 minutes. A silent OTEL collector outage (collector up, ClickHouse down) must be caught.
- **Operator-friendly output**: aligned, colored table with a bold final row. `✓ API ✓ Dashboard ✓ Signoz ✓ Healer ✓ Redpanda ✓ Valkey ✓ Temporal ✓ Postgres → OVERALL: pass`.
- **CI-friendly output**: `--json` flag emits a structured record: `{"overall": "pass", "checks": [{"name":"api","status":"pass","latency_ms":12,"detail":"ready"}, ...]}`.
- **Sensible skip**: `--skip signoz,redpanda` lets a dev on a laptop skip optional profiles without editing the script.
- **Fast**: whole run should complete in under 10 seconds in a healthy stack; each probe has a per-check timeout.
- **Reversible, read-only**: no writes, no restarts, no state changes. Running it 1000 times in a row should be safe.

**Non-Goals:**
- Not a replacement for `healer.py`'s continuous monitoring loop — this is a point-in-time probe.
- Not a performance benchmark — latency is reported for diagnostics, not for alerting. Latency thresholds are NOT checked.
- Not a security scanner — does not verify TLS, certs, auth config, or RLS state.
- Not a workflow engine health probe — we check Temporal is reachable, we do NOT check task queue backlog or workflow success rates (`zvadmin diagnose` already owns that).
- Not a Kubernetes probe. k8s has its own `readinessProbe`/`livenessProbe` manifests; this script is designed for the Compose stack.
- No package installation. If `jq` is missing the script fails fast with a clear message, it does not apt-install.
- Not a corruption detector. Postgres `pg_isready` only verifies the socket accepts connections — we don't scan tables for drift (that's `zvadmin model-check`).

## Decisions

### D1 — One monolithic bash script, not a modular Python tool
The audit-round-2-fixes batch committed to keeping shell scripts strict (`set -euo pipefail`, `curl -fsS`, `jq`). A single `bash` file is visible in a code review, trivially `diff`-able, and has zero install burden. A Python rewrite would pull in a venv or system packages — unacceptable for air-gap. We stick with bash; each check is a function of the form `check_<service>() -> (status, latency_ms, detail)`.

**Alternative considered**: port `zvadmin diagnose` to cover these probes. Rejected — `zvadmin` is a Go binary that needs to be cross-built and shipped; `stack_healthcheck.sh` should work on a fresh-clone machine with zero build step.

### D2 — Per-check function signature + a central runner
Each probe is a bash function that sets three globals (`CHECK_STATUS`, `CHECK_LATENCY_MS`, `CHECK_DETAIL`) via an in-function helper `emit pass|fail|skip|degraded "<detail>"`. The runner iterates over an ordered list `CHECKS=("api" "dashboard" "signoz" "healer" "redpanda" "valkey" "temporal" "postgres")`, skipping any listed in `--skip`, and appends to a `RESULTS` array. This gives us one single place to add a new probe, and the table / JSON renderer reads from the same `RESULTS` array.

### D3 — Signoz "trace ingestion" check uses the services API with a 10-minute window
Simply probing `GET :3301/api/v1/health` would miss the failure mode "collector is up and returning 200, but ClickHouse write failed and no spans land in the database". We additionally `GET :3301/api/v1/services?start=<now-10m>&end=<now>` and assert the response is a non-empty JSON array. Rationale: a working collector with even one instrumented service (the Zovark worker always emits spans via `stages.tracing`) must report at least itself in a 10-minute lookback. If the response is empty, we emit `degraded` (not `fail`) because there's a legitimate cold-start window.

**Alternative**: call the Signoz query service directly to count raw rows in ClickHouse. Rejected — requires auth cookie + the Signoz login flow, too fragile. The `/api/v1/services` endpoint is unauthenticated when the Signoz instance is the local compose one and is stable across versions.

**Alternative**: emit a test span from the script itself via `curl` to the OTLP HTTP collector, then poll for it. Rejected — requires building a protobuf or JSON OTLP payload by hand, dependency nightmare for air-gap. Services endpoint is the right cost/benefit.

### D4 — TCP probes use `bash -c '</dev/tcp/host/port'` with a `timeout` wrapper
For Redpanda (`:19092`) and a Temporal fallback, we don't want to require `nc` (BSD and GNU variants differ wildly) or `ncat`. `bash` has native `/dev/tcp` which works on every GNU+BSD bash. We wrap it in `timeout 5 bash -c '...' 2>/dev/null` so a hung listener can't block the script. On MinGW/Git-Bash, `/dev/tcp` works in bash built-in; the workaround is already shipped in the repo's other scripts.

### D5 — Container-dependent probes go through `docker compose exec` with a presence check
For Valkey, Temporal (`tctl`), and Postgres (`pg_isready`), we `docker compose exec -T <service> <command>`. Before any such check the script runs `docker compose ps -q <service>` to confirm the container exists and is running; if not, the check is marked `skip` not `fail`. This gives the right answer when someone ran `docker compose up api worker` without `postgres` (e.g., a test stack pointing at an external DB).

**Alternative**: assume the containers are running. Rejected — produces a wall of red when the stack is intentionally partial.

### D6 — Exit codes: 0 = pass, 1 = hard fail, 2 = degraded
- `0`: every non-skipped check returned `pass`.
- `1`: at least one check returned `fail` (a required dependency is down).
- `2`: no `fail`, but at least one `degraded` (e.g., Signoz `/api/v1/health` OK but `services` returns empty — cold start window, or trace pipeline hiccup). CI can treat this as a warning.
- Skips do not affect exit code.

### D7 — Colors are opt-out, not opt-in
Default behavior: emit ANSI colors if stdout is a TTY (`[ -t 1 ]`). `--no-color` forces plain text. `--json` implies `--no-color` and emits a single JSON object, one RESULT line per probe + overall. This matches the pattern set by `kubectl`, `gh`, `rg`.

### D8 — Configurable via env vars with fallback to compose-local defaults
Each base URL is an env var with the compose-local default: `ZOVARK_API_BASE=${ZOVARK_API_BASE:-http://127.0.0.1:8090}`, etc. The probe never hardcodes `localhost` (Alpine IPv6 resolution pitfall — we saw this in the audit). `REDIS_PASSWORD` is required for the Valkey probe if unset (after the audit-round-2-fixes infra changes the default is `hydra-redis-dev-2026` but we don't assume it).

### D9 — Retry policy: one shot
Each probe gets one chance with a 5-second timeout. No exponential backoff, no retry loop. The script is a point-in-time probe, not a startup waiter. If someone wants a startup waiter they can wrap it in `until scripts/stack_healthcheck.sh; do sleep 5; done`.

### D10 — Skip list is additive via `--skip a,b,c`
We do not provide `--only a,b` because the primary use case is "everything except the profiles I didn't start". An operator who wants one check should just `curl` that one endpoint.

## Risks / Trade-offs

- **[Risk] False positive on Signoz during cold start.** → Mitigation: services-API check emits `degraded` not `fail`, which maps to exit 2. CI can be configured to ignore exit 2.
- **[Risk] `/dev/tcp` on MinGW Git-Bash.** → Mitigation: the repo's other scripts (`scripts/backup-db.sh`, `scripts/apply_migrations.sh`) already rely on bash built-ins including `/dev/tcp`; we document that Windows users need Git-Bash ≥2.0.
- **[Risk] `docker compose exec` fails when the operator has `COMPOSE_PROJECT_NAME` set non-standardly.** → Mitigation: script uses `docker compose ps` to discover the container, never hardcodes the name. We document `COMPOSE_PROJECT_NAME` behavior in the script header.
- **[Risk] jq not installed.** → Mitigation: hard-fail at script start with clear message + install hint (`apt install jq` / `brew install jq`). Same pattern as `smoke_test_100.sh` after audit 5.19.
- **[Risk] ANSI escapes show up in logs/CI output.** → Mitigation: auto-detect TTY + `--no-color` flag. CI already pipes stdout so auto-detection degrades gracefully.
- **[Risk] False negative on Temporal because `tctl cluster health` prints on stderr vs stdout depending on version.** → Mitigation: redirect `2>&1`, grep for `SERVING` OR successful exit code, accept either.
- **[Risk] The script drifts from reality as new services are added.** → Mitigation: `CHECKS` is a single array at the top of the file — adding a new probe is one function + one array append. Document this pattern at the top of the script.
- **[Trade-off] No concurrent execution.** Running probes sequentially keeps the script simple and avoids bash `wait -n` portability issues. In a healthy stack the whole run is under 10 seconds; parallelising would save ~3 seconds at the cost of much harder debugging.

## Migration Plan

1. **Implementation** (single PR): add `scripts/stack_healthcheck.sh` with exec bit; update `CLAUDE.md` "How to Run" section with a one-line pointer; wire into `.github/workflows/ci.yml` after `docker compose up` step as a gating check.
2. **Rollout**: no staged rollout needed — the script is read-only and doesn't change any running service. It either exits 0 or it doesn't.
3. **Post-merge**: operators switch to `scripts/stack_healthcheck.sh` as their first troubleshooting step. Old ad-hoc curl commands in runbooks are replaced.
4. **Rollback**: `git revert` the PR. Zero runtime impact.

## Open Questions

- **Q1**: Should `zvadmin diagnose` shell out to this script, or should they stay independent? → **Recommendation**: independent. `zvadmin` is Go and shells out to `docker exec` already, it can own its own probe logic. The bash script is the "first line" tool; `zvadmin` is the "deep inspect" tool.
- **Q2**: Should the script also check `worker-metrics` and `temporal-exporter` when the `monitoring` profile is up? → **Recommendation**: add them behind `--include monitoring` in a follow-up. Not in scope for the initial PR.
- **Q3**: Should we add a `--wait` mode that retries until pass or timeout? → **Recommendation**: defer. Easy to add as a wrapper if needed: `until scripts/stack_healthcheck.sh --quiet; do sleep 5; done`.
- **Q4**: Where does the script live in the repo — `scripts/` or `scripts/admin/`? → **Recommendation**: `scripts/` alongside `hardware_check.sh`, `smoke_test_100.sh`, `backup-db.sh`. That's where every other operator script lives today.
- **Q5**: Should we version the JSON output schema? → **Recommendation**: yes, top-level `"schema_version": 1` in the JSON output so future consumers can branch cleanly.
