## ADDED Requirements

### Requirement: Worker consumer discovers new tenant topics within 10 seconds
The worker's Redpanda task consumer SHALL be configured with `metadata_max_age_ms = 10000` (overridable via `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS`), so a `tasks.new.<tenant>` topic created after the consumer started is discovered within 10 seconds. The consumer SHALL also issue a non-blocking `poll(timeout_ms=0)` immediately after subscription so any pre-existing topics are discovered within the first second of startup.

#### Scenario: New tenant topic discovered after consumer is already running
- **WHEN** the worker has been running for several minutes with no tenants, then the API publishes the first message to `tasks.new.<new-tenant-uuid>` (auto-creating the topic)
- **THEN** the worker's consumer assigns the new partition and processes the message within 10 seconds of the publish

#### Scenario: Pre-existing topic discovered on consumer startup
- **WHEN** `tasks.new.<tenant>` already exists in Redpanda before the worker starts, and the worker boots
- **THEN** the consumer discovers the topic within the first second of startup (initial `poll(0)` triggers metadata fetch immediately)

#### Scenario: Override via env
- **WHEN** `ZOVARK_REDPANDA_METADATA_MAX_AGE_MS=5000` is set on the worker container
- **THEN** the consumer uses 5000 ms as the metadata refresh interval

### Requirement: Consumer logs assignment changes
The worker's Redpanda task consumer SHALL log a single line whenever its set of assigned partitions changes, naming the count of partitions and topics. The script SHALL NOT log on every empty poll.

#### Scenario: First topic discovered
- **WHEN** the consumer starts with no topics, then a `tasks.new.<tenant>` topic is created and the consumer's metadata refresh picks it up
- **THEN** a single log line is emitted of the form `[redpanda] consumer assignment changed: 1 partition(s) across 1 topic(s)` followed by the topic names

#### Scenario: Idle consumer is silent
- **WHEN** the consumer's assignment hasn't changed between polls
- **THEN** no log lines are emitted (the consumer remains observably idle)

### Requirement: Dev tenant topic is pre-created by seed_dev.sh
`scripts/seed_dev.sh` SHALL invoke `docker exec <redpanda-container> rpk topic create "tasks.new.00000000-0000-0000-0000-000000000010"` (or equivalent idempotent form) so the dev tenant's task dispatch topic exists from the moment the seed script runs. The call SHALL be idempotent (re-running the seed SHALL NOT raise on an already-existing topic).

#### Scenario: Fresh dev volume
- **WHEN** an operator runs `docker compose down -v && docker compose up -d` and the seed mount executes `seed_dev_data.sql`
- **THEN** the seed script's topic-create call succeeds and `rpk topic list` includes `tasks.new.00000000-0000-0000-0000-000000000010`

#### Scenario: Re-run on already-seeded volume
- **WHEN** an operator runs `scripts/seed_dev.sh` against a stack where the dev tenant topic already exists
- **THEN** the script exits 0 and the topic-create call is a no-op (`--if-exists=ignore` or equivalent)

## MODIFIED Requirements

### Requirement: Postgres stage tracks status transitions
The postgres stage SHALL poll `SELECT status FROM agent_tasks WHERE id = $1` every 500 milliseconds and record the timestamp at which the status first transitions to `investigating` and then to a terminal state (`completed`, `failed`, or `needs_review`). Stage timeout SHALL be 90 seconds or less by default. On timeout, the stage SHALL emit `fail` with a detail string that names the last observed state AND includes a one-line operator hint pointing at the most likely diagnostic (e.g., `stuck at 'pending' for 90s — likely consumer cold start; check 'docker compose logs worker | grep redpanda'`).

#### Scenario: Stuck at pending
- **WHEN** the row is still at `status='pending'` after the stage 3 timeout
- **THEN** the stage emits `fail` with detail starting with `stuck at 'pending' for <N>s` AND containing the substring `consumer cold start` and the substring `docker compose logs worker`

#### Scenario: Stuck at queued
- **WHEN** the row is at `status='queued'` (backpressure) when the timeout expires
- **THEN** the stage emits `fail` with detail starting with `stuck at 'queued' for <N>s` AND containing a hint about backpressure / Temporal queue depth (a different hint from the consumer-cold-start one)

#### Scenario: Normal transition path
- **WHEN** the task moves through `pending → investigating → completed` within the stage timeout
- **THEN** the timeline records two separate rows (`pg.investigating`, `pg.completed`) each with its own latency, and the stage emits `pass`
