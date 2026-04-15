## Why

The user reported `scripts/e2e_probe.sh` stalling: alerts are accepted by the API (Stage 1 `ingest` passes), the message is found in Redpanda by `rpk topic consume` (Stage 2 `redpanda` passes), but the worker never picks it up — Stage 3 `pg.investigating` times out at 90 seconds with `stuck at 'pending' for 90s`. The investigation row sits at `status='pending'` forever; no Temporal workflow is started.

A targeted audit of the publish/consume path identified one root cause and one secondary risk:

### Root cause: kafka-python pattern subscription has a 5-minute metadata refresh window

`worker/redpanda_consumer.py:69` subscribes via:

```python
self._consumer.subscribe(pattern=r"^tasks\.new\..+$")
```

Pattern subscription in `kafka-python` only discovers topics that **exist at the moment of subscription** OR that the consumer learns about during its periodic metadata refresh. The refresh interval is governed by `metadata_max_age_ms`, which defaults to **300_000 ms (5 minutes)**.

Sequence of events on a fresh stack:
1. `docker compose up -d` starts everything. Worker subscribes to `^tasks\.new\..+$`. **No `tasks.new.*` topics exist yet** — Redpanda hasn't seen any publishes.
2. Operator runs `scripts/e2e_probe.sh`. The API gets a login, builds a synthetic alert, calls `publishTaskNew(ctx, "00000000-0000-0000-0000-000000000010", ...)` which writes to `tasks.new.00000000-0000-0000-0000-000000000010`. Redpanda auto-creates the topic because the API's writer has `AllowAutoTopicCreation: true` (`api/redpanda.go:46`).
3. `rpk topic consume tasks.new.<tenant> --offset end --num 20` succeeds because rpk reads by exact name — it sees the new topic immediately.
4. The worker's pattern-subscribed consumer **does not yet know the topic exists**. Its next metadata refresh will pick it up in (worst case) 5 minutes. The probe's Stage 3 budget is 90 seconds, so it times out long before the worker discovers the topic.
5. The pg row stays at `status='pending'` because no consumer ever dequeues the message.

This explains the symptom precisely: **publish path green, broker green, consumer green-but-deaf, worker silent, pg stuck**. It's not a Temporal bug, not an OTEL bug, not a network bug. It's a one-line consumer config gap.

### Secondary risk: the worker's poll loop swallows pattern-subscription errors silently

`worker/redpanda_consumer.py:79-89` runs:

```python
while not self._stop.is_set():
    try:
        batches = self._consumer.poll(timeout_ms=1000)
        for _tp, records in batches.items():
            ...
    except Exception as e:
        logger.error("Redpanda poll error", error=str(e))
        if self._stop.wait(2):
            break
```

When the pattern subscription doesn't match anything yet, `batches` is `{}` and the loop happily idles. There is **no log line** indicating "subscribed but waiting for topics" or "metadata refresh is in N seconds". An operator looking at `docker compose logs worker` sees only the one-time `Redpanda task consumer thread started` line and assumes the consumer is healthy. It is — it just hasn't discovered the topic yet.

## What Changes

- **Set `metadata_max_age_ms=10000` on the consumer** (`worker/redpanda_consumer.py`). Discovery latency drops from 5 minutes to ≤10 seconds. The probe's 90-second Stage 3 budget then has 9× headroom.
- **Override via env**: `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS` (default `10000`). Production deployments with hundreds of tenants and slow disks may want to dial it down further or up; the env makes that adjustable without a rebuild.
- **Force an initial metadata refresh + poll immediately after subscription** so the consumer discovers any pre-existing `tasks.new.*` topics within the first second of startup, not after the first refresh interval. Tiny ergonomic win for operators restarting the worker after the API has been running.
- **Log subscription state on every metadata change** so operators see `[redpanda] subscribed: tasks.new.… (N topics)` whenever the consumer's set of assigned topics changes. Turns the silent waiting into an observable signal.
- **Pre-create the dev tenant topic in the seed flow** so even a brand-new dev volume has `tasks.new.00000000-0000-0000-0000-000000000010` ready before the worker starts. Achieved by adding one `rpk topic create tasks.new.<dev-tenant-uuid>` call to `scripts/seed_dev.sh` (already an idempotent operator helper).
- **Update `scripts/e2e_probe.sh` Stage 3 detail string** to include the consumer's last-known metadata age so a future stall has a clear pointer ("worker hasn't refreshed metadata in 47s — consumer in cold-start window?").
- **Add an `ingest-stall` runbook section** to `docs/RUNBOOK_HEALTHCHECK.md` documenting the failure mode, the diagnostic commands, and the fix.

## Capabilities

### New Capabilities

- (none — this change extends the existing `e2e-pipeline-probe` and modifies the worker's consumer behaviour)

### Modified Capabilities

- `e2e-pipeline-probe`: the Stage 3 `pg.investigating` detail string now mentions consumer metadata age on stall, helping operators diagnose ingest-pipeline lag without grepping logs.

## Impact

- **Affected code**: `worker/redpanda_consumer.py` (consumer config + initial-poll bootstrap + subscription log line), `scripts/seed_dev.sh` (one new `rpk topic create` call), `scripts/e2e_probe.sh` (Stage 3 detail enrichment), `docs/RUNBOOK_HEALTHCHECK.md` (new section).
- **Runtime impact**: the worker's consumer now refreshes broker metadata every 10 seconds instead of every 5 minutes. Negligible network cost (a single Kafka `MetadataRequest` per refresh = ~few KB).
- **Risk**: low. Lowering `metadata_max_age_ms` is the documented kafka-python tuning for "I want pattern subscription to discover new topics quickly". The pre-create call in `seed_dev.sh` is a no-op when the topic already exists (`rpk topic create` is idempotent with `--if-not-exists`).
- **Breaking**: none. Every change is additive or a configuration relaxation.
