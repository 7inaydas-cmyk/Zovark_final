## ADDED Requirements

### Requirement: Single executable e2e probe script
The repository SHALL ship an executable script `scripts/e2e_probe.sh` that, when invoked without arguments, submits one synthetic SIEM alert to the running stack, tracks it through every pipeline stage, and prints a per-stage timeline. The script SHALL be committed with mode 0755.

#### Scenario: Script is executable
- **WHEN** a contributor clones the repo and runs `test -x scripts/e2e_probe.sh`
- **THEN** the command exits with status 0

#### Scenario: Default invocation completes end-to-end on a healthy stack
- **WHEN** the probe runs against a freshly started stack and every pipeline component is healthy
- **THEN** the probe completes within the default timeout, prints a timeline whose final row reads `OVERALL: pass`, and exits with status 0

### Requirement: Probe stages are strictly ordered and individually measured
The probe SHALL execute the following stages in order, measuring latency for each: (0) setup, (1) ingest, (2) redpanda, (3) postgres status transition, (4) signoz span-count delta, (5) verdict persisted, (6) cleanup tag. Each stage SHALL have its own timeout budget. A failure in any stage SHALL be named explicitly in a `STALL:` line before the overall exit code is determined.

#### Scenario: Stall at postgres status transition
- **WHEN** the task row is stuck in `queued` state when Stage 3's timeout expires
- **THEN** the output includes a `STALL: stage 3 (pg.investigating)` line naming the last observed state, subsequent stages are marked `skip`, and the exit code is 1

#### Scenario: Each stage row has a hop latency
- **WHEN** the probe runs successfully
- **THEN** the timeline table includes a per-row `HOP` column showing the time elapsed since the previous stage completed, plus an absolute `AT` timestamp and a cumulative `+ms` since Stage 0

### Requirement: Ingest stage submits a synthetic alert via /api/v1/tasks
The ingest stage SHALL `POST /api/v1/tasks` with a JSON body containing `task_type="probe_noop"`, `input.synthetic=true`, `input.probe_id=<UUID>`, `input.trace_id=<same UUID>`, and a minimal `siem_event` object. The response task_id SHALL be captured and used as the join key for every subsequent stage.

#### Scenario: Synthetic flag is set on the inserted row
- **WHEN** the ingest stage succeeds
- **THEN** `SELECT (input->>'synthetic')::boolean FROM agent_tasks WHERE id = <task_id>` returns `true`

#### Scenario: Probe ID is persisted
- **WHEN** the ingest stage succeeds
- **THEN** `SELECT input->>'probe_id' FROM agent_tasks WHERE id = <task_id>` returns the probe UUID

### Requirement: Redpanda stage confirms message arrival in tasks.new.{tenant}
The redpanda stage SHALL invoke `docker exec <redpanda-container> rpk topic consume tasks.new.<tenant> --offset end --num 20 --format json` and assert that at least one of the returned messages contains the ingest stage's task_id. The stage SHALL retry up to 3 times with 1-second backoff before marking failure.

#### Scenario: Message is found on the first consume attempt
- **WHEN** the task_id appears in the first 20-message consume batch
- **THEN** the stage emits `pass` with a detail line naming the topic and offset

#### Scenario: Message is not found after 3 retries
- **WHEN** three consecutive `rpk topic consume` calls complete without finding the task_id
- **THEN** the stage emits `fail` and subsequent stages are skipped

### Requirement: Postgres stage tracks status transitions
The postgres stage SHALL poll `SELECT status FROM agent_tasks WHERE id = $1` every 500 milliseconds and record the timestamp at which the status first transitions to `investigating` and then to a terminal state (`completed`, `failed`, or `needs_review`). Stage timeout SHALL be 90 seconds or less by default.

#### Scenario: Normal transition path
- **WHEN** the task moves through `pending → investigating → completed` within the stage timeout
- **THEN** the timeline records two separate rows (`pg.investigating`, `pg.completed`) each with its own latency

#### Scenario: Task stuck in queued state
- **WHEN** the task is still in `queued` when Stage 3's timeout expires
- **THEN** the stage emits `fail` with detail `timeout — stuck at 'queued' for <N>s`

### Requirement: Signoz stage verifies span-count delta for both services
The Signoz stage SHALL capture baseline span counts for `zovark-api` and `zovark-worker` during Stage 0 (setup) and, after the verdict is persisted, re-query `/api/v1/services` and assert both counts have increased. The stage SHALL emit `pass` when both counts increase, `degraded` when only one does, and `fail` when neither does.

#### Scenario: Both services emit new spans
- **WHEN** both zovark-api and zovark-worker span counts are strictly greater than their Stage 0 baselines
- **THEN** the stage emits `pass`

#### Scenario: Only zovark-api emits new spans
- **WHEN** zovark-api span count increases but zovark-worker count does not
- **THEN** the stage emits `degraded` with detail `zovark-worker: no new spans` and the overall exit code is 2

#### Scenario: Signoz unreachable with --signoz-required false
- **WHEN** the probe is invoked with `--signoz-required false` and Signoz returns a non-2xx response
- **THEN** the stage emits `degraded` and the overall exit code is not affected by this stage alone

### Requirement: Verdict stage asserts non-null verdict and risk_score
The verdict stage SHALL query `SELECT output->>'verdict', output->>'risk_score' FROM agent_tasks WHERE id = $1` and assert the verdict is non-null. It SHALL emit `pass` when the verdict is `benign` with `risk_score=5` (the `probe_noop` fixed verdict), and `degraded` for any other non-null verdict.

#### Scenario: Expected benign verdict
- **WHEN** the query returns `verdict='benign'` and `risk_score='5'`
- **THEN** the stage emits `pass`

#### Scenario: Unexpected verdict
- **WHEN** the query returns a non-null verdict other than `benign/5`
- **THEN** the stage emits `degraded` with detail naming the actual verdict and risk_score

### Requirement: Cleanup stage tags the probe row
The cleanup stage SHALL `UPDATE agent_tasks SET input = jsonb_set(input, '{_probe_cleanup}', 'true'::jsonb) WHERE id = $1` so operator dashboard queries can filter out probe rows from default views. The row SHALL NOT be deleted.

#### Scenario: Cleanup tag is set
- **WHEN** the cleanup stage completes successfully
- **THEN** `SELECT input->>'_probe_cleanup' FROM agent_tasks WHERE id = $1` returns `true`

#### Scenario: --skip-cleanup disables the tag
- **WHEN** the probe is invoked with `--skip-cleanup`
- **THEN** the cleanup stage is marked `skip` and the row is left untouched for debugging

### Requirement: Probe completes or times out within 120 seconds
The whole probe SHALL complete or emit a timeout failure within the configured total timeout (default 120 seconds, overridable via `--timeout`). On timeout, the exit code SHALL be 1 and the output SHALL include a `STALL:` line naming the stage at which the timeout was reached.

#### Scenario: Default timeout
- **WHEN** the probe is invoked without `--timeout` and any stage takes longer than 120 seconds in total
- **THEN** the probe terminates, emits a `STALL:` line, and exits with status 1

#### Scenario: Custom timeout
- **WHEN** the probe is invoked with `--timeout 30`
- **THEN** the probe uses 30 seconds as the total budget

### Requirement: probe_noop investigation plan in the worker
The worker SHALL include a `probe_noop` entry in `worker/tools/investigation_plans.json` that runs `extract_ipv4` + `map_mitre` against the synthetic siem_event and produces `verdict=benign, risk_score=5`. The plan SHALL NOT invoke any LLM.

#### Scenario: probe_noop plan exists and is deterministic
- **WHEN** the probe submits a task with `task_type="probe_noop"`
- **THEN** the worker loads the `probe_noop` plan, runs it without an LLM call, and produces the expected verdict within ~1 second on a warm cache

### Requirement: stack_healthcheck.sh --e2e flag chains the probe
`scripts/stack_healthcheck.sh` SHALL accept a `--e2e` flag. When the flag is set, the script SHALL run all existing probes first and then chain `scripts/e2e_probe.sh --json` as the final probe, folding its result into the overall exit code. Without `--e2e`, the healthcheck behaves identically to its pre-change baseline.

#### Scenario: --e2e chains successfully
- **WHEN** `scripts/stack_healthcheck.sh --e2e` runs against a healthy stack
- **THEN** every existing probe plus a final `e2e` row appear in the output table, and the overall exit code reflects both

#### Scenario: --e2e failure bubbles to overall exit code
- **WHEN** `e2e_probe.sh` exits 1 but every other probe passed
- **THEN** `stack_healthcheck.sh --e2e` exits with status 1

### Requirement: Machine-readable JSON output
The probe SHALL accept a `--json` flag. When set, it SHALL emit a single JSON object of the form `{"schema_version": 1, "probe_id": ..., "overall": "pass"|"fail"|"degraded", "total_ms": ..., "stall_stage": null|<name>, "stages": [{"name": ..., "status": ..., "latency_ms": ..., "hop_ms": ..., "detail": ...}, ...]}`. `--json` SHALL imply `--no-color`.

#### Scenario: JSON output is valid
- **WHEN** the probe runs with `--json` and passes end-to-end
- **THEN** the output is a single JSON object whose `overall == "pass"` and whose `stages` array has one entry per executed stage

### Requirement: Read-only safety outside the intended side effect
Apart from the one tagged row inserted into `agent_tasks` during the run, the probe SHALL NOT write to any other table, restart any container, modify any file, or mutate any shared state. It SHALL be safe to run repeatedly without cleanup between runs.

#### Scenario: Running the probe N times creates N tagged rows and nothing else
- **WHEN** the probe is invoked 5 times in succession
- **THEN** `SELECT count(*) FROM agent_tasks WHERE (input->>'synthetic')::boolean IS TRUE AND created_at > NOW() - INTERVAL '1 hour'` returns 5, and no other tables have been modified
