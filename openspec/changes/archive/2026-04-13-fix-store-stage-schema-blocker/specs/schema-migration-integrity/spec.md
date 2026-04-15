## ADDED Requirements

### Requirement: schema_migrations ledger MUST reflect physical schema

The `schema_migrations` ledger row for a given migration file MUST only exist if every DDL object that file creates (tables, columns, indexes, types, functions, policies) is physically present in the live PostgreSQL schema. A row whose claimed objects are absent is a **lying ledger row** and MUST be repaired before the migration runner is trusted on that volume.

#### Scenario: Lying ledger row blocks Stage 5 Store
- **WHEN** `schema_migrations` contains `('040_human_review_flags.sql', 'init_sql', '2026-01-01', …)` but `agent_tasks` has neither `needs_human_review` nor `review_reason` columns
- **THEN** the operator MUST treat the ledger row as invalid, MUST apply the migration's DDL directly, and MUST rewrite the ledger row with `source='manual_backfill'`, `applied_at=now()`, `applied_by=CURRENT_USER`

#### Scenario: Verification before trusting any ledger row
- **WHEN** an operator suspects ledger drift on a specific migration filename
- **THEN** the operator MUST run object-presence queries (`information_schema.columns`, `to_regclass`, `pg_indexes`) for every object that file creates, and MUST classify the row as "lying" if any object is absent

### Requirement: Stage 5 Store dependencies MUST exist before any investigation completes

`worker/stages/store.py` writes `agent_tasks.needs_human_review`, `agent_tasks.review_reason`, and rows into `llm_audit_log`. These objects MUST exist in the live schema for Stage 5 Store to succeed. Their absence MUST be treated as a P1 dev blocker.

#### Scenario: Missing review columns block all investigation completion
- **WHEN** `agent_tasks.needs_human_review` does not exist
- **THEN** every `InvestigationWorkflowV2` execution SHALL fail at Stage 5 with `column "needs_human_review" of relation "agent_tasks" does not exist`, the task row SHALL remain `status='pending'` indefinitely, and the dashboard SHALL show no completions

#### Scenario: Missing llm_audit_log degrades silently then errors
- **WHEN** `llm_audit_log` table does not exist
- **THEN** the worker SHALL log `Validation failure logging failed (non-fatal): relation "llm_audit_log" does not exist` and the Store stage SHALL still attempt the main write, but the LLM audit trail SHALL be lost

### Requirement: Repair MUST be idempotent and minimally scoped

A ledger-repair operation MUST use `IF NOT EXISTS` on every DDL statement, MUST run the DDL plus the ledger UPDATE in a single transaction, and MUST touch only the specific filenames being repaired. Bulk reapplication of unrelated migrations is forbidden in a repair operation.

#### Scenario: Repair transaction is atomic
- **WHEN** an operator repairs migration 040
- **THEN** the DDL (ALTER TABLE … ADD COLUMN, CREATE INDEX) and the `UPDATE schema_migrations` SHALL be wrapped in a single `BEGIN; … COMMIT;` block, so a failure in either half rolls back the whole repair

#### Scenario: Repair never touches unrelated migrations
- **WHEN** the operator is repairing 040 and 046
- **THEN** no other ledger row SHALL be modified, and no other migration file SHALL be applied, even if other rows are also suspected of being lies

### Requirement: Repair MUST be verified by a real pipeline submission

After a Stage-5-related ledger repair, the operator MUST submit one synthetic `brute_force` task via `POST /api/v1/tasks` and MUST observe it reach `status='completed'` before declaring the repair successful. Schema-presence checks alone are insufficient.

#### Scenario: Successful end-to-end verification
- **WHEN** the operator submits a `brute_force` task with the standard CLAUDE.md curl recipe after repair
- **THEN** within 60 seconds the task SHALL transition to `status='completed'`, the worker logs SHALL NOT print `Store failed`, and `agent_tasks.output` SHALL contain a non-null verdict

#### Scenario: Failed verification rolls back the change
- **WHEN** the verification submission does not reach `status='completed'` within 60 seconds, or the worker logs print any new `Store failed` line
- **THEN** the repair SHALL be marked failed, the operator SHALL collect the worker logs and `agent_tasks` row, and SHALL NOT mark the change as done

### Requirement: Repair MUST be auditable via source='manual_backfill'

Every repaired `schema_migrations` row MUST set `source='manual_backfill'` so future operators can list all drift repairs with a single query. Reusing `source='init_sql'` or `source='migration_runner'` for a repair is forbidden because it perpetuates the audit lie.

#### Scenario: Audit query lists all repairs
- **WHEN** an operator runs `SELECT filename, applied_at, applied_by FROM schema_migrations WHERE source='manual_backfill' ORDER BY applied_at`
- **THEN** the result SHALL contain every ledger row that was ever repaired due to physical drift, with the actual repair time and the operator who ran it
