## Why

The existing stack verification surface is a ladder of probes that each answer a narrow question: `stack_healthcheck.sh` asserts every container is up; `smoke_test_100.sh` asserts 100 synthetic alerts produce the expected verdicts; the pending `healthcheck-fixes-and-telemetry` change adds a `telemetry` probe that asserts Signoz has seen a span from each Zovark service in the last ten minutes. None of them answer the one question that matters at 3 AM: **is a single alert submitted to the API right now making it all the way through to a verdict, with OTEL spans from every hop, in real time?**

That question has seven components (HTTP admission, Redpanda publish, worker consume, Temporal workflow, Postgres row transition, OTEL span flush on both sides, verdict write-back), and any one of them can be broken while every other probe lies and says "green". We need a single end-to-end probe that pushes one alert through the whole pipeline, watches it land, and reports *where it stalled* on failure — not just "overall: fail".

The `healthcheck-fixes-and-telemetry` change (in flight) already owns the container-name discovery rewrite and the generic `telemetry` probe, so this change does NOT duplicate that work. This change builds on top of it.

## What Changes

- **Add** `scripts/e2e_probe.sh` — a single-shot end-to-end pipeline verification. Executable (0755), strict bash, `jq`-driven output.
- **Flow** (each stage has its own timeout, own latency measurement, own detail column):
  1. **Stage 0: setup** — Generate a 36-char UUID `PROBE_ID`, record the wall-clock start, and capture Signoz's per-service span counts for `zovark-api` and `zovark-worker` as baselines. Log in to the API and obtain a Bearer token.
  2. **Stage 1: ingest** — POST `/api/v1/tasks` with a synthetic SIEM alert whose `input.synthetic=true`, `input.probe_id=<PROBE_ID>`, `task_type=probe_noop`, and a `trace_id` field that equals `PROBE_ID`. Record the returned `task_id` and latency.
  3. **Stage 2: redpanda** — Use `rpk topic consume tasks.new.<tenant_id> --offset end --num 20 --format json` (via `docker exec zovark-redpanda`) to scan the last ~20 messages and confirm one contains `task_id=<task_id>`. Record the hop latency (time since Stage 1 complete).
  4. **Stage 3: postgres status transitions** — Poll `SELECT status FROM agent_tasks WHERE id = $1` every 500ms. Record timestamps for each observed state: `pending/queued → investigating → completed/needs_review/failed`. Timeout if the task is still in an early state after 90 seconds.
  5. **Stage 4: signoz traces** — Query Signoz `/api/v1/services` for both `zovark-api` and `zovark-worker` and verify the per-service span count increased compared to the Stage 0 baseline. Additionally, attempt a best-effort trace lookup by `zovark.trace_id` via `/api/v1/traces/<hex-encoded-probe-id>` — soft-fails to just the count-increase check when the attribute search isn't indexed.
  6. **Stage 5: verdict** — `SELECT output->>'verdict', output->>'risk_score' FROM agent_tasks WHERE id = $1` and assert the verdict is non-null. Record the final verdict + risk score.
  7. **Stage 6: cleanup** — `UPDATE agent_tasks SET input = jsonb_set(input, '{_probe_cleanup}', 'true') WHERE id = $1` so the row is tagged for operator cleanup. We never DELETE so operators can inspect probe runs in the dashboard; `_probe_cleanup=true` tells the dashboard to filter them from default views. Drop any temporary Redpanda offset bookkeeping.
- **Timeline output** — Print a colored table with one row per stage showing: stage name, timestamp (HH:MM:SS), latency-since-start, hop-latency (time between this stage and the previous one), status (pass/fail/skip), detail. Final row is `OVERALL: pass|fail` plus the total wall-clock and the stalled stage if applicable.
- **Timeout contract** — Whole probe exits non-zero after 120 seconds if the verdict has not been written. The failure row names the **stalled stage** explicitly (e.g., `stall: waiting for worker to transition task to 'investigating' (stuck at 'queued' for 85s)`). This is the crux: the probe's value is that it tells you *exactly* where the pipeline is broken.
- **Exit codes**: `0` end-to-end pass, `1` stalled or failed at a stage, `2` pass with degraded signals (e.g., Signoz span count increased but trace-attribute lookup was empty — still a legitimate warm cache).
- **Flags** — `--json` (machine-readable output), `--no-color`, `--timeout N` (default 120), `--tenant <id>` (default = decode from login response), `--signoz-required {true,false}` (default true — set false for stacks running without the tracing profile), `--skip-cleanup` (leave the probe row untouched for debugging), `--help`.
- **Env vars** — `ZOVARK_API_BASE`, `ZOVARK_SIGNOZ_BASE`, `ZOVARK_REDIS_PASSWORD`, `ZOVARK_PROBE_EMAIL` (default `admin@test.local`), `ZOVARK_PROBE_PASSWORD` (default `TestPass2026`), `ZOVARK_PROBE_TASK_TYPE` (default `probe_noop`).
- **Database access** — Reuse the `_compose_container_name` helper from the in-flight `healthcheck-fixes-and-telemetry` change so the probe doesn't re-implement container discovery. When that change isn't applied yet, fall back to `docker ps --filter "name=zovark-postgres"`.
- **Worker support** — Add a lightweight `probe_noop` investigation plan to `worker/tools/investigation_plans.json` that runs two deterministic tools (`extract_ipv4`, `map_mitre`) against a synthetic SIEM event, produces a fixed `verdict=benign, risk_score=5` verdict, and completes in well under a second. This ensures the probe doesn't accidentally burn LLM tokens and isn't subject to LLM availability.
- **stack_healthcheck integration** — Add a `--e2e` flag to `scripts/stack_healthcheck.sh` that, when passed, runs `scripts/e2e_probe.sh --json` after all other probes and folds its result into the overall exit code. Without `--e2e`, the healthcheck behaves exactly as before.
- **Cleanup hooks** — Add a daily maintenance query documented in `docs/RUNBOOK_HEALTHCHECK.md` that deletes probe rows older than 30 days: `DELETE FROM agent_tasks WHERE (input->>'synthetic')::boolean AND created_at < NOW() - INTERVAL '30 days'`. Not enforced automatically — this is an operator decision.
- **Scoping clarification** — This change does NOT duplicate the container-name-discovery or `telemetry`-probe work from `healthcheck-fixes-and-telemetry`. The `_compose_container_name` helper is expected to already exist when this change lands. If the two changes are applied out of order, the apply step in this change creates a minimal shim.

## Capabilities

### New Capabilities

- `e2e-pipeline-probe`: A single-shot end-to-end verification script that submits one synthetic alert and tracks it through ingest → Redpanda → worker → Postgres → Signoz → verdict, printing a per-hop timeline and exiting non-zero with a named stall point on failure.

### Modified Capabilities

- `stack-healthcheck` (owned by the in-flight `healthcheck-fixes-and-telemetry` change): this change ADDS a `--e2e` flag that optionally chains the e2e probe onto the existing healthcheck run. The flag is purely additive; the default run is unchanged.

## Impact

- **Affected code**: new `scripts/e2e_probe.sh`, modifications to `scripts/stack_healthcheck.sh` (add `--e2e` flag only), new entry in `worker/tools/investigation_plans.json` (`probe_noop` plan), updates to `CLAUDE.md` "How to Run" and `docs/RUNBOOK_HEALTHCHECK.md`.
- **Data impact**: each `e2e_probe.sh` run writes ONE row to `agent_tasks` with `input.synthetic=true` and `_probe_cleanup=true`. Over a year of daily probes, ~365 rows. Dashboard queries filter out synthetic rows by default (`WHERE NOT (input->>'synthetic')::boolean IS TRUE`) — small addition to the existing WHERE clauses.
- **Dependencies**: `jq` (already required), `docker` + `docker compose ps` (already required by stack_healthcheck), `rpk` inside the `zovark-redpanda` container (already installed by the redpanda image).
- **Runtime impact on the live pipeline**: one extra synthetic alert per probe run. `probe_noop` takes ~200ms end-to-end so the load is negligible — comparable to one Juice Shop hit.
- **Risk**: medium. The probe writes to the real `agent_tasks` table and reads from the real Redpanda topic. It's designed to be safe (synthetic flag, cleanup tag, fast no-op tools, no LLM calls) but operators running it against a production stack should understand it creates real rows.
- **Breaking**: none. Every change is additive. The `probe_noop` plan lives alongside the existing 24 plans. The `--e2e` flag is opt-in. The probe row is tagged, not deleted.
