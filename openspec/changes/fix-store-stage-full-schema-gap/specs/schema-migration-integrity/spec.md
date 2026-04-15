## ADDED Requirements

### Requirement: Drift repair MUST be driven by code-derived gap analysis

When investigating a Stage 5 Store failure (or any DB-write failure caused by suspected ledger drift), the operator MUST extract the complete set of consumed schema objects from the source file of the failing stage by reading every SQL statement in that file and listing each table and column referenced. The operator MUST then verify each referenced object against `information_schema.columns` and `information_schema.tables`. The migration list to apply MUST be the smallest set of files that creates the missing objects from this verified gap list. Hand-maintained or rumour-derived "list of probably-missing migrations" MUST NOT be used as the source of truth.

#### Scenario: Operator follows code-derived analysis to find next blocker
- **WHEN** an operator hits `Store failed: column "X" of relation "Y" does not exist` in worker logs
- **THEN** the operator SHALL read `worker/stages/store.py` (or the equivalent failing-stage source file) end-to-end, SHALL extract every SQL statement and its referenced columns/tables, SHALL audit each reference against `information_schema`, and SHALL produce a gap list with each missing object mapped to the migration file that creates it

#### Scenario: Repair scope is bounded by the gap list
- **WHEN** the operator builds `repair.sql`
- **THEN** the file SHALL contain DDL only for migration files that create objects in the gap list, plus the corresponding `UPDATE schema_migrations` statements, and SHALL NOT include DDL from migrations whose objects do not appear in the gap list

#### Scenario: Hard blockers vs non-fatal gaps are classified
- **WHEN** classifying gap-list entries
- **THEN** the operator SHALL distinguish between objects whose absence causes an uncaught exception in the failing stage (HARD BLOCKERS — the call site is not wrapped in try/except) and objects whose absence is silently logged (NON-FATAL — the call site is wrapped in try/except), and SHALL document this classification in the change's design document

### Requirement: Code/schema forks are NOT ledger drift and MUST NOT be repaired by inventing migrations

When Phase A reveals that a referenced table exists in the live database but with a fundamentally different shape than the source code expects (different primary key type, mutually exclusive column sets, parallel definitions in `init.sql` and a numbered migration), this is a **code/schema fork**, NOT ledger drift. A ledger-repair change MUST NOT attempt to fix forks by adding columns that exist in neither `init.sql` nor any migration file. Forks require a separate change with a code-vs-DB design decision.

#### Scenario: investigation_memory fork is documented as out of scope
- **WHEN** Phase A reveals that `init.sql:136` defines `investigation_memory` with `(id uuid, ..., embedding vector(768))` while `migrations/045_investigation_memory.sql` defines it with `(id serial, task_type, alert_signature, code_template, ...)` and `worker/stages/store.py:_save_pattern` writes the migration-045 column names against the live init.sql shape
- **THEN** the ledger-repair change SHALL document this as out of scope, SHALL NOT add `task_type`/`alert_signature`/`code_template`/`iocs_found`/`findings_found`/`success` columns to the live `investigation_memory` table, and SHALL leave the non-fatal `Pattern save failed` warning in place pending a separate code-vs-DB reconciliation change

#### Scenario: Operator does not invent hybrid DDL
- **WHEN** a referenced column does not exist in any migration file
- **THEN** the operator SHALL classify it as a code/schema fork, SHALL document it in the proposal's "Out of scope" section, and SHALL NOT write `ALTER TABLE ... ADD COLUMN` statements that have no migration-file source

### Requirement: Repair MUST include a dry-run-via-ROLLBACK gate before commit

Before applying any drift-repair `repair.sql`, the operator MUST run the same DDL inside a transaction that ends with `ROLLBACK` rather than `COMMIT` to verify that every statement is syntactically valid and constraint-compatible against the current live schema. The dry run MUST be a separate psql invocation from the real apply.

#### Scenario: Dry-run catches a constraint violation before commit
- **WHEN** the operator runs the dry-run version of `repair.sql`
- **THEN** the output SHALL be inspected for `ERROR:` lines, and the operator SHALL NOT proceed to the real apply if any `ERROR:` line is present

#### Scenario: Dry-run is the same DDL as the real apply
- **WHEN** the operator builds the dry-run version
- **THEN** the only differences from the real-apply version SHALL be the trailing `ROLLBACK` instead of `COMMIT`, with no other statement removed or modified
