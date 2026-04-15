## Why

Right now, "is the stack up?" has no single authoritative answer. `curl /ready` only covers the Go API's three dependencies (PG + Redis + Temporal). The tracing profile, Redpanda, the healer, Signoz trace ingestion, and the dashboard aren't probed by any one command. The audit-round-2-fixes batch just tightened SSE, dedup, port bindings, and dashboard nginx — we need a repeatable, CI-friendly way to assert all of that is actually running after a `docker compose up -d`, without asking the operator to paste 9 `curl` commands.

`scripts/hardware_check.sh` verifies the host BEFORE install; there is nothing that verifies the stack AFTER install. This change adds that script.

## What Changes

- **Add** `scripts/stack_healthcheck.sh` — a single executable that verifies every runtime component of the Zovark stack in one invocation and prints a colored pass/fail table.
- Probes:
  - **Go API** — `GET :8090/ready` must return 200 with `{"status":"ready"}`.
  - **Dashboard nginx** — `GET :3000/health` must return 200 (new endpoint added in audit-round-2-fixes 4.6).
  - **Signoz frontend** — `GET :3301/api/v1/health` must return 200; script also verifies recent trace ingestion by querying the signoz services API (`/api/v1/services`) and confirming at least one service was seen in the last 10 minutes, so a silent OTEL outage is caught.
  - **Healer** — `GET :8081/api/health` must return 200.
  - **Redpanda** — TCP connect to `:19092` must succeed (Kafka bootstrap listener). Optional: `rpk cluster info` via `docker compose exec` when available.
  - **Valkey** — `docker compose exec redis valkey-cli -a "$REDIS_PASSWORD" ping` must return `PONG`.
  - **Temporal** — `docker compose exec temporal tctl --address temporal:7233 cluster health` must report `SERVING`; fallback: TCP connect to `:7233`.
  - **PostgreSQL** — `docker compose exec postgres pg_isready -U zovark -d zovark` must return exit 0.
- Colored table output: `✓` in green / `✗` in red / `~` in yellow for degraded; final row `OVERALL: pass/fail`.
- Strict shell: `set -euo pipefail`, `curl -fsS --max-time 5` everywhere, `jq` for JSON assertions.
- Exit codes: `0` when every check passes, `1` if any hard-required check fails, `2` when only optional/trace-ingestion checks are degraded.
- Respects `ZOVARK_API_BASE`, `ZOVARK_DASHBOARD_BASE`, `ZOVARK_SIGNOZ_BASE`, `ZOVARK_HEALER_BASE`, `REDIS_PASSWORD` env overrides so it works from inside CI, a dev laptop, or inside a Kubernetes exec session.
- Command-line flags: `--json` (machine-readable output), `--no-color`, `--skip signoz,redpanda` (skip specific probes when those profiles aren't running), `--timeout 5`.
- **Executable bit**: script is committed with mode 0755 (`chmod +x`).

## Capabilities

### New Capabilities

- `stack-healthcheck`: A single-command stack probe covering API, dashboard, Signoz (with trace ingestion), healer, Redpanda, Valkey, Temporal, and Postgres, with both human-readable (colored table) and machine-readable (JSON) output. Returns structured exit codes so it can be wired into `healer`, CI, and operator runbooks.

### Modified Capabilities

- None.

## Impact

- **Affected code**: new file `scripts/stack_healthcheck.sh`. No changes to running services, containers, or Compose manifests.
- **New tooling dependencies**: `jq` (already required by `smoke_test_100.sh` after audit-round-2-fixes 5.19), `curl`, `nc`/`bash -c '/dev/tcp/…'` for TCP probes. No Python, no Docker SDK.
- **Docs**: `CLAUDE.md` "How to Run" section gains a one-liner pointer to `scripts/stack_healthcheck.sh`. `docs/HARDWARE_REQUIREMENTS.md` is untouched.
- **CI**: integration job can invoke the script after `docker compose up` to gate on stack health before running the smoke corpus — replaces the ad-hoc curl block currently at `.github/workflows/ci.yml:208+`.
- **Runbook**: operator troubleshooting starts with `scripts/stack_healthcheck.sh` instead of nine individual curl commands.
- **Risk**: low. The script is read-only — no writes, no container restarts, no state mutation. Worst case a probe reports a false negative when the target service is still starting (mitigated by retry + timeout flags).
