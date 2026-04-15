## ADDED Requirements

### Requirement: Probe inserts a Stage 0.6 schema_ledger between db_write and ingest
`scripts/e2e_probe.sh` SHALL define a new function `stage06_schema_ledger()` that runs after `stage05_db_write()` and before `stage1_ingest()` in the stage runner sequence. Stage 0.6 SHALL be added to the script's `usage()` / banner block so the documented stage list reads `0`, `0.5`, `0.6`, `1`, `2`, `3`, `4`, `5`, `6`, `7`. The stage runner array near `e2e_probe.sh` line ~772 SHALL include `stage06_schema_ledger` between `stage05_db_write` and `stage1_ingest`.

#### Scenario: Stage 0.6 runs after Stage 0.5 on a healthy stack
- **WHEN** an operator runs `scripts/e2e_probe.sh` on a healthy stack with the ledger in sync
- **THEN** the timeline shows Stage 0.5 `db_write` immediately followed by Stage 0.6 `schema_ledger`, both with `pass` status, before Stage 1 `ingest` runs

#### Scenario: Stage 0.6 fails — Stage 1 is skipped, not attempted
- **WHEN** Stage 0.6 records `fail`
- **THEN** Stage 1 `ingest` records `skip` with detail `prior stage failed`, no HTTP request is sent to `POST /api/v1/tasks`, and the script exits non-zero
