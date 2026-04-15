## Context

The Zovark pipeline has seven moving parts on the happy path (HTTP admission → Redpanda publish → worker consume → Temporal workflow → Postgres insert + status transitions → OTEL span emission on both sides → verdict write-back), plus auxiliary paths (SSE push, alert dedup, batch buffer, backpressure). Every existing verification surface answers a narrow question:

- `curl /ready` → "API's three dependencies are reachable"
- `scripts/stack_healthcheck.sh` → "every container is up"
- `scripts/stack_healthcheck.sh` with the `telemetry` probe (once `healthcheck-fixes-and-telemetry` lands) → "Signoz has seen a span from zovark-api and zovark-worker in the last 10 minutes"
- `scripts/smoke_test_100.sh` → "100 canned alerts produce correct verdicts"
- `zvadmin diagnose` → "operational dashboards are healthy"

None of them answer: **I just pushed an alert to this stack; did it come out the other side?** And none of them tell you *where* it stalled when it didn't.

The audit found that most real outages are NOT "something is down" (which existing probes catch) but "something is up but silently not forwarding". Examples we've already seen:
- Redpanda writer connected but the worker consumer group was rebalancing.
- Worker dequeued the task but hung on LLM init because the semaphore loop-binding bug (audit 2.8) left `_fast_semaphore` attached to a dead event loop.
- Temporal workflow ran and committed but the SSE NOTIFY never fired because the `NOTIFY payload > 8000 bytes` truncation dropped the event silently (audit 2.22).
- Signoz collector was healthy but ClickHouse was full, so no spans landed — and `/api/v1/services` still returned Signoz's self-instrumentation as "proof" the pipeline worked.

Every one of those is caught by an end-to-end probe that submits an alert and tracks it through every stage, because the stall point is the diagnostic answer. The per-probe detail column names the stall: `stuck at 'queued' for 85s — worker consumer lag?` turns a 30-minute triage into a 30-second triage.

Stakeholders: on-call (page response), CI (pre-deploy smoke), operators (post-upgrade verification), healer (future: can shell out to this script as its deep-health probe).

## Goals / Non-Goals

**Goals:**
- **One alert, one probe, one answer**. Submit a synthetic alert; emit a colored timeline; exit 0 iff the alert reaches a verdict with traces confirmed on both sides.
- **Name the stall**. On failure, the output says *exactly* which hop the packet stopped at — not "overall: fail".
- **No LLM dependency**. The probe must work on stacks without OpenAI keys, without llama.cpp, without the `tracing` profile. A `probe_noop` investigation plan runs purely deterministic tools.
- **Safe to run in production**. Synthetic alert is flagged, verdict is fixed, cleanup tag is set — operators can filter probe rows from dashboard queries and delete them on a rolling 30-day window.
- **Composable**. `scripts/stack_healthcheck.sh --e2e` chains the probe after the existing checks. Standalone, `scripts/e2e_probe.sh` can be invoked from CI, the healer, or a manual terminal.
- **No new runtime dependencies**. `jq` + `docker` + `curl` — everything is already in the operator's toolbelt.
- **Timeout-bounded**. Whole probe finishes or errors in ≤120 seconds by default, configurable via `--timeout`.

**Non-Goals:**
- Not a load test. One alert, one run.
- Not a replacement for `smoke_test_100.sh`. The 100-alert script covers verdict calibration across 30 attack types; this probe covers the single-flow pipeline itself.
- Not a verdict-correctness check. The probe asserts the pipeline moved the alert; the `probe_noop` plan has a *fixed* verdict, so "verdict is what we asked for" is not a useful signal here. Calibration is `smoke_test_100.sh`'s job.
- Not a way to invoke the LLM. The probe must pass on a stack with `ZOVARK_MODE=templates-only` or zero LLM config.
- Not a way to test the alert dedup layer. The probe's UUID is unique per run so dedup always routes through. Dedup testing stays in the separate dedup stress-test harness.
- Not a way to test multi-tenancy. The probe uses the default login's tenant. Cross-tenant isolation testing belongs in the test suite, not a live probe.
- Not a way to exercise SSE push specifically. We poll the database instead — simpler, no reconnection logic.
- Does NOT own container-name discovery or the `telemetry` probe. Those belong to `healthcheck-fixes-and-telemetry`.

## Decisions

### D1 — `probe_noop` investigation plan in `investigation_plans.json`
The probe must have a deterministic, fast path. We add a new `probe_noop` entry to `worker/tools/investigation_plans.json`:

```json
"probe_noop": {
  "plan": [
    { "step": 1, "tool": "extract_ipv4", "args": { "text": "$raw_log" } },
    { "step": 2, "tool": "map_mitre",    "args": { "task_type": "probe_noop" } }
  ],
  "description": "e2e probe — deterministic no-op flow; fixed verdict=benign",
  "expected_verdict": "benign",
  "expected_risk_score": 5
}
```

The assess stage sees a benign verdict because `extract_ipv4` returns an empty list, `map_mitre` returns an empty technique list, and the default verdict-derivation for "no findings + no IOCs" is `benign`. No LLM calls; no flaky dependencies; ~50ms of pure Python + two DB writes.

**Alternative considered:** re-use the existing `benign-system-event` skill. Rejected — that path runs through `benign` classification logic which the audit reshaped, and we want the probe to exercise the v3 tool-runner path, not the benign-fast-fill shortcut.

### D2 — Redpanda probe uses `rpk topic consume --offset end --num 20`
`rpk` is installed inside the `zovark-redpanda` image, so `docker exec zovark-redpanda rpk topic consume tasks.new.<tenant> --offset end --num 20 --format json` works without adding any toolchain. We consume the last 20 messages (not just the last 1) because another tenant could be pushing concurrently, and grep for `"task_id":"<our-task-id>"`. If not found after one probe call, we retry twice with a 1-second gap, then emit `fail` for Stage 2.

**Alternative considered:** plumb a dedicated Kafka client library into bash. Rejected — `rpk` is already there.

**Alternative considered:** tail Redpanda via the Redpanda admin HTTP API. Rejected — that requires setting up a Redpanda user + ACL, `rpk exec` doesn't.

### D3 — Postgres polling loop uses the `_compose_container_name` helper
The probe shells out via `docker exec <container-name> psql -U zovark -d zovark -t -c "..."`. Container discovery uses the helper from `healthcheck-fixes-and-telemetry`; when that change isn't applied, the probe falls back to `docker ps --filter "name=zovark-postgres"`. Polling interval is 500ms with a 90-second cap for Stage 3 alone; if the task is still in `pending` or `queued` after 90s we emit `fail` with a detail string naming the last observed state.

### D4 — Signoz verification is a span-count delta, not an attribute search
Before submitting the alert we capture the current `data[].dataPoints[].num_calls` (or equivalent) for both `zovark-api` and `zovark-worker` from `/api/v1/services`. After the verdict is stored (Stage 5), we re-query and assert both counts increased by ≥1. This is the simplest observable that proves BOTH services are exporting spans AND the collector wrote them to ClickHouse.

We *additionally* attempt a best-effort `trace_id` attribute search via `/api/v1/traces?service=zovark-api&traceID=<probe_id>` — but this path depends on Signoz's search-by-trace-id endpoint being enabled and the `zovark.trace_id` attribute being indexed. When the search endpoint is unavailable, we report `degraded` for the telemetry portion but still pass if the count delta check passed.

**Alternative considered:** query ClickHouse directly. Rejected — requires ClickHouse client binary + password-plaintext-in-script.

**Alternative considered:** rely solely on the `telemetry` probe from `healthcheck-fixes-and-telemetry`. Rejected — that probe uses a 10-minute window so a fresh e2e run might not cause a visible delta without strict ordering. The e2e probe's baseline-vs-post comparison is tighter.

### D5 — Timeline table format
The final output is a fixed-width table with one row per stage:

```
E2E Pipeline Probe — probe_id: 8f2a6e…
--------------------------------------------------------------------
  STAGE               AT         +ms    HOP     STATUS  DETAIL
--------------------------------------------------------------------
  0 setup            13:45:02.100   0       -       ok    token obtained, signoz baseline captured
  1 ingest           13:45:02.312  212    212ms     ok    task_id=d3…, HTTP 201
  2 redpanda         13:45:02.483  383    171ms     ok    found in tasks.new.<tenant>@offset 4712
  3 pg.investigating 13:45:03.091  991    608ms     ok    queued→investigating
  4 pg.completed     13:45:03.472 1372    381ms     ok    completed
  5 signoz           13:45:04.015 1915    543ms     ok    zovark-api +3 spans, zovark-worker +2 spans
  6 verdict          13:45:04.089 1989     74ms     ok    verdict=benign risk_score=5
  7 cleanup          13:45:04.132 2032     43ms     ok    _probe_cleanup flag set
--------------------------------------------------------------------
OVERALL: pass  total: 2032ms
```

On failure, the row of the stalled stage is `fail` red + the remaining stages are `-` gray, and a `STALL:` line names the stage and the observable state at timeout.

### D6 — Cleanup tag, not delete
We tag the row with `_probe_cleanup=true` (jsonb_set into `input`) instead of DELETE so operators can inspect probe results in the dashboard after the fact. Dashboard queries filter via `WHERE (input->>'synthetic')::boolean IS NOT TRUE` — the filter lives in one place (`api/task_handlers.go listTasksHandler`) and gets a 1-line addition in this change.

**Alternative considered:** full DELETE. Rejected — loses audit trail for debugging "why did the probe fail at 3 AM?".

**Alternative considered:** write to a separate `probe_runs` table. Rejected — adds a migration, adds schema drift risk, and fragments the "one pipeline, one table" story.

### D7 — Timeout + stall reporting
Each stage has its own max-wait (e.g., Stage 3 postgres poll = 90s, Stage 4 signoz delta = 15s). The whole probe has a `--timeout 120` cap. On timeout:
- The current stage is marked `fail` with detail `timeout (N s) — last state: <observed>`.
- Every subsequent stage is marked `skip`.
- The overall row is `fail` with `STALL: stage <N> (<name>)`.
- The exit code is 1.

The stall line is the most important piece — it's what an operator reads first. Format: `STALL: stage 3 (pg.investigating) — still in 'queued' after 85.3s. Check: worker consumer lag, Temporal queue depth.`

### D8 — `--e2e` flag on `stack_healthcheck.sh`
`stack_healthcheck.sh --e2e` runs every existing probe first, then chains `e2e_probe.sh --json`. The result is merged into the final JSON `checks` array as a single probe named `e2e` with `status = e2e_probe.sh's overall`. Exit code rolls up: if `e2e_probe` returns 1 or 2, that bubbles to the healthcheck's overall. The flag is opt-in because the e2e probe has side effects (a row in agent_tasks) and the healthcheck should stay read-only by default.

### D9 — Login-based token acquisition
The probe logs in via `POST /api/v1/auth/login` with `ZOVARK_PROBE_EMAIL` + `ZOVARK_PROBE_PASSWORD`. On failure it emits `fail` at Stage 0 and exits — there's no point tracking an alert you can't submit. The token is used as `Authorization: Bearer <token>` for every subsequent API call.

### D10 — `--signoz-required false` escape hatch
Dev stacks often run without the `tracing` profile. When `--signoz-required false` is passed, Stage 4 still runs but a failure there is classified `degraded`, not `fail`, and does NOT affect the overall exit code. The span-count delta check is skipped entirely if Signoz is unreachable.

## Risks / Trade-offs

- **[Risk] Probe row pollutes agent_tasks.** → Mitigation: `input.synthetic=true` flag, `_probe_cleanup=true` tag, 30-day cleanup query documented in runbook. Dashboard default filter added.
- **[Risk] Redpanda `rpk topic consume --num 20` may miss the message under heavy concurrent load.** → Mitigation: retry up to 3 times with 1-second backoff; on persistent miss, emit `fail` at Stage 2 with `possible consumer lag or topic drift` detail.
- **[Risk] Signoz span-count delta may be 0 on an instrumentation-off run where everything still "works" (verdict is written but no traces are emitted).** → Accepted: that's exactly what the telemetry check is supposed to catch. Operators who disable OTEL can pass `--signoz-required false`.
- **[Risk] `probe_noop` task type collision with a future real task type.** → Mitigation: the probe uses a reserved prefix `probe_noop` that's never a real SIEM task type. Documented in the runbook.
- **[Risk] The probe writes synthetic data to the production tenant.** → Mitigation: `--tenant` flag lets operators target a dedicated probe tenant. Default is the logged-in user's tenant.
- **[Risk] 120-second timeout is too short for stacks under heavy load.** → Mitigation: `--timeout N` override.
- **[Risk] `probe_noop` plan runs inside Temporal and counts against the backpressure limit.** → Accepted: one extra workflow per probe run is well inside the 200-soft / 1000-hard threshold.
- **[Trade-off] Tagging instead of deleting probe rows means the table grows slowly over time.** → Accepted: runbook documents the 30-day cleanup query; operators can schedule it in pg_cron or a k8s CronJob.
- **[Trade-off] The probe uses `docker exec` for Redpanda + Postgres queries, which requires Docker CLI access.** → Accepted: same constraint as `stack_healthcheck.sh`.

## Migration Plan

1. **Single PR**: land `scripts/e2e_probe.sh` + `worker/tools/investigation_plans.json` addition + `scripts/stack_healthcheck.sh --e2e` flag + `CLAUDE.md` note + `docs/RUNBOOK_HEALTHCHECK.md` section in one change.
2. **Ordering with `healthcheck-fixes-and-telemetry`**: ideally that change lands first so `_compose_container_name` is already in place. If this change lands first, `e2e_probe.sh` uses its own inlined container-discovery fallback (a slightly simpler version of the `docker compose ps --format json` helper) and the stack_healthcheck `--e2e` flag code path is gated on the flag's presence.
3. **Post-merge**: run `scripts/e2e_probe.sh` against a running stack. Every stage should reach `pass` within 5 seconds on a warm cache, 10 seconds on a cold start.
4. **Rollback**: `git revert`. The only DB-observable side effect is tagged synthetic rows which can be left in place or deleted at operator discretion.

## Open Questions

- **Q1**: Should the probe exit 0 when Signoz spans aren't visible but every other stage passed? → **Recommendation**: exit 2 (degraded). The spans might be in-flight due to BatchSpanProcessor delay; exit 2 lets CI distinguish "pipeline broken" from "pipeline working but trace flush was slow".
- **Q2**: Should the probe write to a separate `probe_runs` table for easier cleanup? → **Recommendation**: no, keep it in `agent_tasks` with the tag. Covered in D6.
- **Q3**: Should we expose a probe mode that uses Temporal's built-in query API to watch the workflow state instead of polling the DB? → **Recommendation**: defer. The DB poll is simpler and tells the same story. Temporal query adds a gRPC dependency.
- **Q4**: Should the healer fold this probe into its continuous monitoring? → **Recommendation**: defer to a follow-up. The probe is designed to be idempotent and safe to run repeatedly, so it's eligible, but wiring it into healer requires testing the restart-on-fail behaviour carefully.
- **Q5**: Should we fail the CI integration suite on a stalled e2e_probe? → **Recommendation**: yes, in a follow-up PR that chains `stack_healthcheck.sh --e2e` from `.github/workflows/ci.yml`. Not in scope for this change.
- **Q6**: What does `probe_noop` do if the worker's investigation_plans.json is stale (container not rebuilt)? → **Recommendation**: the probe emits `fail` at Stage 3 with `unknown task_type 'probe_noop' in worker`. The runbook documents the `docker compose build worker && docker compose up -d worker` step as the fix.
