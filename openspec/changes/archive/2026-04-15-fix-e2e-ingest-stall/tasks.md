## 1. worker/redpanda_consumer.py — consumer config + initial poll + log

- [x] 1.1 Add `import os` if not already present (it is — line 11)
- [x] 1.2 In `RedpandaTaskConsumer._run`, add a `metadata_max_age_ms` kwarg to the `KafkaConsumer(...)` constructor reading from `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS` with default `10000`
- [x] 1.3 Immediately after `self._consumer.subscribe(pattern=...)`, call `self._consumer.poll(timeout_ms=0)` once to force an initial metadata fetch + topic discovery
- [x] 1.4 Add an instance attribute `self._last_assignment: frozenset[str] = frozenset()` to track the previously-seen partition set
- [x] 1.5 At the top of every iteration of the poll loop, compute `current = frozenset(f"{tp.topic}:{tp.partition}" for tp in self._consumer.assignment())`. When `current != self._last_assignment`, log a single line `[redpanda] consumer assignment changed: <N> partition(s) across <M> topic(s): <comma-separated topic names>` and update `self._last_assignment = current`. Use the existing `logger.info` helper.
- [x] 1.6 Confirm `python3 -m py_compile worker/redpanda_consumer.py` exits 0

## 2. scripts/seed_dev.sh — pre-create the dev tenant topic

- [x] 2.1 Add a `_resolve_redpanda_container` helper to `scripts/seed_dev.sh` that mirrors `_resolve_pg_container`, trying `docker compose ps -q redpanda` first then falling back to `docker ps --filter 'name=^zovark-redpanda$'`
- [x] 2.2 Add a `_create_dev_tenant_topic` function that resolves the redpanda container and runs `docker exec -i <container> rpk topic create "tasks.new.00000000-0000-0000-0000-000000000010"` with `--if-exists=ignore` (or equivalent — verify the rpk subcommand syntax via `docker exec zovark-redpanda rpk topic create --help`)
- [x] 2.3 Call `_create_dev_tenant_topic` from `mode_seed` after the psql seed runs successfully; non-zero exit from the topic create SHALL be a warning (not a hard fail) so operators with the redpanda profile not running can still seed the DB
- [x] 2.4 Update the `--check` mode to verify the topic exists via `docker exec <container> rpk topic list | grep -F "tasks.new.00000000-0000-0000-0000-000000000010"` and report it as a third row in the fixture table
- [x] 2.5 Update the `--help` text to document the new behaviour

## 3. scripts/e2e_probe.sh — Stage 3 detail enrichment

- [x] 3.1 In `stage3_pg_investigating`, when the timeout expires and `last_state` is `'pending'`, emit detail: `stuck at 'pending' for <N>s — likely consumer cold start; check 'docker compose logs worker | grep redpanda'`
- [x] 3.2 When `last_state` is `'queued'`, emit detail: `stuck at 'queued' for <N>s — likely backpressure; check Temporal queue depth and ZOVARK_MAX_PENDING_WORKFLOWS`
- [x] 3.3 Other stuck states keep the existing generic `stuck at '<state>' for <N>s` detail

## 4. docs/RUNBOOK_HEALTHCHECK.md — ingest stall section

- [x] 4.1 Add a new section "Ingest stall: rows stuck at `pending`" after the existing healer + signoz sections
- [x] 4.2 Document the symptom (`pg.investigating` Stage 3 timeout, row at `status='pending'`)
- [x] 4.3 Document the root cause (kafka-python pattern subscription + 5-minute default `metadata_max_age_ms`)
- [x] 4.4 Document the fix-applied state (`metadata_max_age_ms=10000`, initial `poll(0)`, dev tenant topic pre-created)
- [x] 4.5 Document the diagnostic commands:
        - `docker compose logs worker | grep redpanda`
        - `docker exec zovark-redpanda rpk topic list`
        - `docker exec zovark-redpanda rpk group describe zovark-task-workers`
        - `docker exec zovark-postgres psql -U zovark -d zovark -c "SELECT id, status FROM agent_tasks WHERE input->>'probe_id' IS NOT NULL ORDER BY created_at DESC LIMIT 5;"`
- [x] 4.6 Document the operator escape hatch: `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS=5000 docker compose up -d worker`

## 5. Verification

- [x] 5.1 `python3 -m py_compile worker/redpanda_consumer.py` exits 0
- [x] 5.2 `bash -n scripts/seed_dev.sh` exits 0
- [x] 5.3 `bash -n scripts/e2e_probe.sh` exits 0
- [x] 5.4 `scripts/seed_dev.sh --help` prints the updated usage
- [x] 5.5 Code-grep: `metadata_max_age_ms` appears exactly once in `worker/redpanda_consumer.py` and references `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS`
- [x] 5.6 Code-grep: `rpk topic create` appears in `scripts/seed_dev.sh` exactly once, gated on the dev tenant UUID
- [x] 5.7 Code-grep: the new Stage 3 detail strings (`consumer cold start`, `backpressure`) appear in `scripts/e2e_probe.sh`
- [x] 5.8 Manual live-stack run (operator): `docker compose down && docker compose up -d && scripts/seed_dev.sh && scripts/e2e_probe.sh` reaches Stage 6 within 30 seconds and exits 0
- [x] 5.9 Manual live-stack run (operator): `docker compose logs worker | grep 'consumer assignment changed'` shows one line per topic discovery (at most one line per metadata refresh window)
