## 1. probe_noop investigation plan

- [x] 1.1 Add a `probe_noop` entry to `worker/tools/investigation_plans.json` that runs `extract_ipv4` on `$raw_log` and `map_mitre` with `task_type="probe_noop"`, tagged with `"description": "e2e probe — deterministic no-op flow"`
- [x] 1.2 Confirm via `python3 -c "import json; d=json.load(open('worker/tools/investigation_plans.json')); assert 'probe_noop' in d"` that the JSON file parses and the new key is present
- [x] 1.3 Update `CLAUDE.md` Attack Types / Plans table with one row noting `probe_noop` is reserved for the e2e probe script

## 2. scripts/e2e_probe.sh scaffold

- [x] 2.1 Create `scripts/e2e_probe.sh` with shebang `#!/usr/bin/env bash`, `set -euo pipefail`, `MSYS_NO_PATHCONV=1`, and a header comment block listing every stage, exit code, env var, and flag
- [x] 2.2 `chmod +x scripts/e2e_probe.sh` so the committed file is mode 0755
- [x] 2.3 Add a jq-presence guard and a `docker` / `docker compose` presence guard at the top of the script
- [x] 2.4 Define env var defaults: `ZOVARK_API_BASE`, `ZOVARK_SIGNOZ_BASE`, `ZOVARK_PROBE_EMAIL`, `ZOVARK_PROBE_PASSWORD`, `ZOVARK_PROBE_TASK_TYPE`, `ZOVARK_REDIS_PASSWORD`
- [x] 2.5 Parse CLI flags: `--json`, `--no-color`, `--timeout N`, `--tenant ID`, `--signoz-required {true,false}`, `--skip-cleanup`, `--help`
- [x] 2.6 Implement TTY-aware colors (reuse the pattern from `stack_healthcheck.sh`) and the `_emit_info` / `_emit_ok` / `_emit_warn` / `_emit_fail` helpers

## 3. Stage primitives and helpers

- [x] 3.1 Implement `_now_ms` (portable millisecond epoch — GNU `date +%s%3N` with python3 and seconds fallback, same as `stack_healthcheck.sh`)
- [x] 3.2 Implement `record_stage <name> <status> <detail>` that appends to a `STAGES` array with (name, status, latency-from-start, hop-from-prev, detail)
- [x] 3.3 Implement `_resolve_container <service>` that first tries the shared `_compose_container_name` helper from `healthcheck-fixes-and-telemetry` (if present via source), then falls back to `docker compose ps --format json | jq …`, then falls back to `docker ps --filter "name=zovark-<service>"`
- [x] 3.4 Implement `_pg_exec <sql> [<args>...]` that resolves the postgres container and runs `docker exec <container> psql -U zovark -d zovark -t -A -c "<sql>"`
- [x] 3.5 Implement `_rpk_tail <topic> <num>` that resolves the redpanda container and runs `docker exec <container> rpk topic consume <topic> --offset end --num <num> --format json`

## 4. Stage 0 — setup

- [x] 4.1 Generate `PROBE_ID` via `uuidgen` (fallback: `python3 -c 'import uuid; print(uuid.uuid4())'`)
- [x] 4.2 Record `PROBE_START_MS = $(_now_ms)` and push a Stage 0 row
- [x] 4.3 Log in to the API via `POST /api/v1/auth/login` with `ZOVARK_PROBE_EMAIL` / `ZOVARK_PROBE_PASSWORD`; extract `token` + `user.tenant_id` from the JSON response
- [x] 4.4 Capture Signoz baselines: `SIGNOZ_API_BASELINE` and `SIGNOZ_WORKER_BASELINE` from `GET $ZOVARK_SIGNOZ_BASE/api/v1/services?start=<now-10m>&end=<now>` — if Signoz is unreachable and `--signoz-required=true`, emit a Stage 0 fail; if `false`, log a warning and continue with baselines set to 0
- [x] 4.5 Stage 0 emits `pass` with detail `token obtained; tenant=<id>; signoz baselines: api=<n>, worker=<n>`

## 5. Stage 1 — ingest

- [x] 5.1 Build the ingest payload with jq: `{task_type: $tt, input: {prompt: "e2e probe", severity: "low", synthetic: true, probe_id: $pid, trace_id: $pid, siem_event: {title: "e2e probe", raw_log: "probe alert from scripts/e2e_probe.sh", source_ip: "127.0.0.1", rule_name: "probe_noop"}}}`
- [x] 5.2 `POST $ZOVARK_API_BASE/api/v1/tasks` with the Bearer token; capture `TASK_ID` from the response and assert non-empty
- [x] 5.3 Record the returned trace header `X-Zovark-Trace-ID` for cross-reference
- [x] 5.4 Stage 1 emits `pass` with detail `task_id=<id> http=201` or `fail` with detail `http=<code> body=<head>`

## 6. Stage 2 — redpanda

- [x] 6.1 Call `_rpk_tail "tasks.new.$TENANT_ID" 20` three times with a 1-second backoff between attempts
- [x] 6.2 For each response, grep for `"task_id":"$TASK_ID"` in the JSON payloads; on first match, record the offset and break
- [x] 6.3 Stage 2 emits `pass` with detail `found in tasks.new.<tenant>@offset <N>` or `fail` with detail `not found after 3 retries`

## 7. Stage 3 — postgres status transitions

- [x] 7.1 Poll `_pg_exec "SELECT status FROM agent_tasks WHERE id = '$TASK_ID'"` every 500ms, with a 90-second per-stage cap
- [x] 7.2 Record the first observation where status != 'pending' AND != 'queued' as the `investigating` transition
- [x] 7.3 Continue polling until a terminal state (`completed`, `failed`, `needs_review`, `error`) or the stage cap fires
- [x] 7.4 Emit two separate stage rows: `3 pg.investigating` (with hop latency from Stage 2) and `4 pg.completed` (with hop latency from Stage 3)
- [x] 7.5 On cap timeout, emit `fail` with detail `timeout — stuck at '<last state>' for <N>s`; set `STALL_STAGE`

## 8. Stage 5 — signoz span-count delta

- [x] 8.1 Re-query `GET $ZOVARK_SIGNOZ_BASE/api/v1/services?start=<now-2m>&end=<now>` after the verdict is stored
- [x] 8.2 Extract `zovark-api` and `zovark-worker` counts via jq and compute deltas against the Stage 0 baselines
- [x] 8.3 Emit `pass` when both deltas are ≥1, `degraded` when exactly one is ≥1 (detail: `missing new spans from <service>`), `fail` when neither is ≥1
- [x] 8.4 When `--signoz-required=false`, downgrade any `fail` at this stage to `degraded` and do not set `STALL_STAGE`
- [x] 8.5 Best-effort: also attempt `GET $ZOVARK_SIGNOZ_BASE/api/v1/traces/<hex-probe-id>` and note success/failure in the detail line (does not change the status)

## 9. Stage 6 — verdict

- [x] 9.1 Run `_pg_exec "SELECT output->>'verdict', output->>'risk_score' FROM agent_tasks WHERE id = '$TASK_ID'"`
- [x] 9.2 Assert verdict is non-null
- [x] 9.3 Emit `pass` when verdict == 'benign' and risk_score == '5' (the `probe_noop` fixed verdict)
- [x] 9.4 Emit `degraded` when verdict is any other non-null value (detail: `unexpected verdict=<v> risk_score=<n>`)
- [x] 9.5 Emit `fail` when verdict is NULL (detail: `verdict not persisted`)

## 10. Stage 7 — cleanup

- [x] 10.1 Unless `--skip-cleanup` was passed, run `_pg_exec "UPDATE agent_tasks SET input = jsonb_set(COALESCE(input, '{}'::jsonb), '{_probe_cleanup}', 'true'::jsonb) WHERE id = '$TASK_ID'"`
- [x] 10.2 When `--skip-cleanup`, emit `skip` with detail `row left untouched for debugging`
- [x] 10.3 Emit `pass` after a successful tag update

## 11. Timeline rendering and output

- [x] 11.1 Compute `OVERALL` from the `STAGES` array: `fail` if any status is `fail`, else `degraded` if any is `degraded`, else `pass`
- [x] 11.2 Compute `STALL_STAGE` as the first stage with status `fail` (or empty when `OVERALL != fail`)
- [x] 11.3 Table renderer: fixed-width columns (STAGE, AT, +ms, HOP, STATUS, DETAIL) with TTY-aware colors
- [x] 11.4 After the table, print `OVERALL: <status>  total: <N>ms`; if `STALL_STAGE` is set, print a `STALL:` line above the overall line naming the stage + its observable state
- [x] 11.5 JSON renderer (`--json`): `{"schema_version":1,"probe_id":...,"overall":...,"total_ms":...,"stall_stage":...,"stages":[…]}` built via `jq -n --arg …`

## 12. Exit codes and timeout

- [x] 12.1 Overall timeout: if the total wall-clock exceeds `--timeout` (default 120), terminate with `OVERALL=fail`, set `STALL_STAGE` to the currently-running stage, and exit 1
- [x] 12.2 Exit 0 when `OVERALL==pass`
- [x] 12.3 Exit 1 when `OVERALL==fail`
- [x] 12.4 Exit 2 when `OVERALL==degraded`

## 13. stack_healthcheck integration

- [x] 13.1 Add a `--e2e` flag to `scripts/stack_healthcheck.sh` that, when set, runs the existing probes first and then chains `scripts/e2e_probe.sh --json`
- [x] 13.2 Merge the e2e_probe's JSON `overall` into the healthcheck's final JSON `checks` array as a single probe named `e2e` with the probe's total_ms as the latency
- [x] 13.3 Roll e2e_probe's exit code into the healthcheck overall: if e2e returns 1 or 2, bubble to the healthcheck exit code via the same pass/degraded/fail rules
- [x] 13.4 Default behaviour (without `--e2e`) is unchanged — the healthcheck stays read-only and does not create probe rows

## 14. Documentation

- [x] 14.1 Update `CLAUDE.md` "How to Run" section: add a one-line pointer to `scripts/e2e_probe.sh` as the canonical "is my alert flowing through right now?" check
- [x] 14.2 Add an "e2e probe" section to `docs/RUNBOOK_HEALTHCHECK.md` (created by `healthcheck-fixes-and-telemetry`) or create it here if that change hasn't landed yet — include: the stage diagram, what each STALL message means, the 30-day cleanup query
- [x] 14.3 Document the 30-day probe-row cleanup query: `DELETE FROM agent_tasks WHERE (input->>'synthetic')::boolean AND created_at < NOW() - INTERVAL '30 days'`
- [x] 14.4 Document the dashboard filter pattern: `WHERE NOT COALESCE((input->>'synthetic')::boolean, false)` so real task lists hide probe rows

## 15. Verification

- [x] 15.1 `bash -n scripts/e2e_probe.sh` exits 0
- [x] 15.2 `scripts/e2e_probe.sh --help` prints usage and exits 0
- [x] 15.3 `scripts/e2e_probe.sh --json --timeout 5` against an UNREACHABLE API (e.g. `ZOVARK_API_BASE=http://127.0.0.1:1`) emits `overall=fail`, `stall_stage=stage 0 (setup)`, and exit code 1
- [x] 15.4 `test -x scripts/e2e_probe.sh` is true in the committed tree
- [x] 15.5 `python3 -c "import json; d=json.load(open('worker/tools/investigation_plans.json')); assert d['probe_noop']['plan'][0]['tool']=='extract_ipv4'"` passes
- [x] 15.6 `scripts/stack_healthcheck.sh --help` lists `--e2e` in the OPTIONS section
- [x] 15.7 Manual dry run against a live stack: `scripts/e2e_probe.sh` must reach `OVERALL: pass` with all 8 stages green within 30 seconds of a warm stack
- [x] 15.8 Manual dry run of `scripts/stack_healthcheck.sh --e2e` against the same live stack: overall exit 0, the `e2e` row appears after the existing 8 probes
- [x] 15.9 After a successful run, confirm `SELECT count(*) FROM agent_tasks WHERE (input->>'_probe_cleanup')::boolean IS TRUE AND id=<task_id_from_run>` returns 1
