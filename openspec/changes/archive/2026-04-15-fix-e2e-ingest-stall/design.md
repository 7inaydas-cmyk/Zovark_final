## Context

The Zovark dispatch path is:

```
client → POST /api/v1/tasks (api)
            ↓
        agent_tasks INSERT (status=pending)
            ↓
        publishTaskNew(ctx, tenant_id, task_id, …)   ← api/redpanda.go:61
            ↓
        Redpanda topic: tasks.new.<tenant_id>        ← key=task_id, value=JSON
            ↓
        worker RedpandaTaskConsumer.poll()           ← worker/redpanda_consumer.py:75
            ↓
        Temporal start_workflow → InvestigationWorkflowV2
            ↓
        agent_tasks UPDATE status=investigating → … → completed
```

When the user reports "alerts not flowing from API through Redpanda to worker", the likely failure stations are: API publish, broker accept, broker store, consumer subscribe, consumer poll, Temporal start, pg update. The audit found the publish/broker side is healthy (the e2e probe Stage 2 confirms `rpk topic consume` sees the message), so the failure is downstream — between Redpanda and the worker's consumer dispatch.

### What I confirmed by reading the code

**API publish (`api/redpanda.go`)**: `kafka.Writer{Addr: kafka.TCP("redpanda:9092"), AllowAutoTopicCreation: true, RequiredAcks: kafka.RequireAll}`. On every `publishTaskNew`, the writer posts to `tasks.new.{tenant_id}` and waits for all in-sync replicas to ack. If the write fails, the caller (e.g. `task_handlers.go:263`) flips the row to `status='failed'` via a detached cleanup context. So a publish failure can't silently leave the row at `status='pending'` — it's always either `pending → published → workflow started` OR `pending → failed`. The fact that the row stays at `pending` means publish succeeded but no consumer ever picked the message up.

**Worker consumer (`worker/redpanda_consumer.py`)**: `KafkaConsumer(bootstrap_servers=hosts, group_id="zovark-task-workers", auto_offset_reset="earliest", enable_auto_commit=True, consumer_timeout_ms=1500)`. After `__init__`, it calls:

```python
self._consumer.subscribe(pattern=r"^tasks\.new\..+$")
```

This is a kafka-python **pattern subscription**. Pattern subscriptions only match topics that the consumer's local metadata cache knows about. The cache is refreshed by the consumer on a fixed interval governed by `metadata_max_age_ms`. The kafka-python default is **300_000 ms (5 minutes)** ([kafka-python docs](https://kafka-python.readthedocs.io/en/master/apidoc/KafkaConsumer.html)).

**Order of events on a fresh stack**:

1. T=0s: `docker compose up -d`. Worker starts. Consumer subscribes via pattern. **No `tasks.new.*` topics exist yet.** Local metadata cache: `[]`.
2. T=0s: Worker enters its poll loop. Every poll returns `{}` because no topics are matched.
3. T=0s..299s: Operator runs `scripts/e2e_probe.sh`. API publishes `tasks.new.<tenant>` → Redpanda auto-creates the topic. The worker's metadata cache is **still stale** — it doesn't know the topic exists.
4. T=90s: Probe's Stage 3 (`pg.investigating`) times out. Row is still at `status='pending'`.
5. T≤300s: Worker's metadata refresh fires. Cache picks up `tasks.new.<tenant>`. Pattern matches. Consumer is reassigned to the new partition. Next `poll()` returns the message. Workflow finally starts. (By this time the probe has already exited with a stall.)

Stakeholders: every developer running `scripts/e2e_probe.sh`; CI's e2e gate; operators doing post-deploy verification; the SOC dashboard's `Submit Investigation` button (same path).

## Goals / Non-Goals

**Goals:**
- **Stop the stall**. After the fix, a fresh `docker compose up -d` followed by an immediate `scripts/e2e_probe.sh` run reaches Stage 3 within 10–12 seconds (one metadata refresh window), well under the 90-second budget.
- **Ship it as a one-line consumer config change** so the fix is reviewable, auditable, and trivially reversible.
- **Add observability**: subscription state is logged on every change, so the next "consumer is silent" mystery is a `docker compose logs worker | grep redpanda` away.
- **Keep the pattern subscription**. The consumer subscribes to `tasks.new.*` so it picks up new tenants automatically. A list of explicit topics would force a worker restart on every tenant onboarding — not acceptable.

**Non-Goals:**
- Not switching transport away from Redpanda. The transport is fine; the consumer config is the bug.
- Not pre-creating topics for every tenant in production. That's an operator decision; we only pre-create the dev tenant in `scripts/seed_dev.sh` so dev/CI is fast.
- Not rewriting the consumer to use `confluent-kafka-python` instead of `kafka-python`. The kafka-python library has the bug we're fixing, but it's also the rest of the worker's Kafka surface; switching libraries is a multi-day rework that's out of scope here.
- Not adding a "wait for first message" mode to `e2e_probe.sh`. The 90-second budget at Stage 3 is generous enough once the metadata refresh fires within 10 seconds.
- Not changing API publish behaviour. The publish path is correct.
- Not changing how Redpanda auto-creates topics. The fix is on the consumer side.

## Decisions

### D1 — `metadata_max_age_ms=10000` on the consumer
Set kafka-python's `metadata_max_age_ms` to 10000 (10 seconds) so pattern-subscription topic discovery happens within at most 10 seconds of a new topic being created. Default is 300_000.

**Trade-off:** more frequent metadata refresh = slightly more network chatter to the broker. A `MetadataRequest` is a few KB and the overhead is negligible compared to even one published message. Documented kafka-python tuning for this exact scenario.

**Override:** `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS` (default 10000). Operators with hundreds of tenants on slow disks can dial it up; CI / dev keeps it tight.

### D2 — Initial `poll(0)` after subscription
Immediately after `subscribe()`, call `self._consumer.poll(timeout_ms=0)` once. This forces a metadata fetch on subscription rather than waiting for the first `metadata_max_age_ms` interval to elapse. Combined with D1 it means a worker restarted while there are pre-existing topics will discover them within milliseconds.

**Alternative considered:** call `consumer.topics()` after subscribe. Rejected — `topics()` returns broker metadata but doesn't trigger pattern reassignment; only `poll()` does that.

### D3 — Log subscription state on every change
After every `poll()`, compute `self._consumer.assignment()` (the set of currently-assigned partitions) and compare to the previous iteration. When the set changes, emit `[redpanda] consumer assignment changed: <N partitions> across <M topics>` with the topic names. This converts "silent waiting" into an observable signal.

We do NOT log every empty poll — that would flood. Only log when the assignment changes.

### D4 — Pre-create the dev tenant topic in `scripts/seed_dev.sh`
The seed script (from `database-seed-system`) already runs idempotent operations against the dev stack. We add one more: `docker exec zovark-redpanda rpk topic create "tasks.new.00000000-0000-0000-0000-000000000010" --if-exists=ignore`.

This means the dev tenant's topic exists from the moment `scripts/seed_dev.sh` runs (which happens during `docker compose up -d` via the seed mount, OR manually after a fresh boot). The worker's pattern subscription discovers it immediately on the next refresh — combined with D1 + D2, that's under 10 seconds even on a cold stack.

**Why only dev tenant**: production tenants are provisioned dynamically; pre-creating their topics would be the operator's job, not the seed script's. The dev tenant is the single hardcoded UUID we know about and own.

### D5 — `e2e_probe.sh` Stage 3 detail enrichment
When Stage 3 times out at `status='pending'`, append a hint to the detail string: `stuck at 'pending' for 90s — likely consumer cold start; check 'docker compose logs worker | grep redpanda'`. This points the next operator at the right diagnostic without forcing them to read the runbook.

### D6 — Don't pre-create the topic on the **API** side
The API's `kafka.Writer` already has `AllowAutoTopicCreation: true`, so first publish always creates the topic. Adding an explicit `CreateTopics` call in the API would be redundant and would couple the API to the Kafka admin API (extra surface area). The fix lives entirely in the consumer + a one-line seed addition.

### D7 — Runbook section
Add an "ingest stall" section to `docs/RUNBOOK_HEALTHCHECK.md` with: (a) the failure-mode description, (b) the diagnostic commands (`rpk topic list`, `docker exec zovark-worker-1 python -c "from kafka import KafkaConsumer; ..."` to inspect consumer assignment, `docker compose logs worker | grep redpanda`), (c) the fix (`scripts/seed_dev.sh` to pre-create the dev tenant topic, or `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS=5000 docker compose up -d worker` to dial in a tighter refresh).

## Risks / Trade-offs

- **[Risk] Lowering `metadata_max_age_ms` to 10s increases broker load.** → Accepted: a `MetadataRequest` every 10 seconds per worker process is negligible. Even with 32 worker activities (`MAX_CONCURRENT_ACTIVITIES`), the total is one `MetadataRequest` per second per worker container, well within Redpanda's per-broker limits.
- **[Risk] `rpk topic create --if-exists=ignore` on a real production tenant could mask a configuration error.** → Mitigation: only run the create call against the dev tenant UUID, never against arbitrary tenants. The seed script is dev-only and the call is gated on the same fixed UUID.
- **[Risk] Initial `poll(0)` blocks if the broker is unreachable.** → Mitigation: `timeout_ms=0` is a non-blocking poll in kafka-python; it triggers metadata fetch but returns immediately. If the broker is unreachable, the next blocking `poll(1000)` in the loop will surface the error.
- **[Risk] Logging assignment-change events floods the log on a stack with hundreds of tenants.** → Accepted: assignment changes only happen on metadata refresh OR rebalance. With 100 tenants, that's at most ~100 lines on first discovery, then quiet. Trivially filterable.
- **[Trade-off] We don't fix the underlying kafka-python pattern-subscription gotcha for other code paths.** → Accepted: this consumer is the only pattern-subscription path in the repo. Future code that uses pattern subscription will need the same `metadata_max_age_ms` tuning, documented in the runbook.
- **[Trade-off] We could rewrite the worker to use `confluent-kafka-python` (no metadata cache gotcha) instead of `kafka-python`.** → Out of scope. Multi-day rework, much bigger surface.

## Migration Plan

1. **Single PR**: ship `worker/redpanda_consumer.py` config change + initial poll + log line, the one new `rpk topic create` call in `scripts/seed_dev.sh`, the `e2e_probe.sh` detail enrichment, and the runbook section.
2. **Post-merge verification**:
   ```bash
   docker compose down && docker compose up -d
   # wait for API health
   scripts/seed_dev.sh                    # pre-creates the dev tenant topic
   scripts/e2e_probe.sh                   # should reach Stage 6 within 10s
   ```
   Alternative for stacks where the seed isn't run: `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS=5000 docker compose up -d worker` and the consumer picks up new tenants within 5s.
3. **Rollback**: `git revert`. Zero runtime impact — the consumer just reverts to the 5-minute refresh.

## Open Questions

- **Q1**: Should `metadata_max_age_ms` be even tighter (5s)? → **Recommendation**: 10s is a good default. Faster than 90-second probe timeout, slow enough that broker chatter is invisible. CI / staging can override via env if they want.
- **Q2**: Should we register the dev tenant topic in `init.sql` or via a worker-side bootstrap? → **Recommendation**: no, keep it in `scripts/seed_dev.sh`. Topic creation is a Redpanda admin operation, not a SQL operation, and the seed script is already the operator-runnable seed orchestrator.
- **Q3**: Should the worker fail-fast if pattern subscription matches nothing for >60s? → **Recommendation**: no. A worker that's correctly idle (no tenants yet) shouldn't crash. The new assignment-change log line makes silent idle observable without forcing fail-fast behaviour.
- **Q4**: Should we also pre-create `tasks.new.<system-tenant>` for the SYSTEM tenant from migration 063? → **Recommendation**: no — the SYSTEM tenant is for break-glass auth, not for task dispatch. No `publishTaskNew` ever targets it.
- **Q5**: Is there a kafka-python release that fixes the gotcha upstream? → **Recommendation**: not aware of one. The behaviour is documented as intentional in kafka-python; the metadata refresh interval is the supported tuning knob.
