## MODIFIED Requirements

### Requirement: Drift repair MUST be driven by code-derived gap analysis

When investigating a Stage 5 Store failure (or any DB-write failure caused by suspected ledger drift), the operator MUST extract the complete set of consumed schema objects from the source file of the failing stage by reading every SQL statement in that file and listing each table and column referenced. The operator MUST then verify each referenced object against `information_schema.columns` and `information_schema.tables`. The migration list to apply MUST be the smallest set of files that creates the missing objects from this verified gap list. Hand-maintained or rumour-derived "list of probably-missing migrations" MUST NOT be used as the source of truth. **A gap is only considered NON-FATAL if (a) the call site is wrapped in a Python `try/except` AND (b) the call site is wrapped in a PostgreSQL `SAVEPOINT` so the surrounding transaction survives a Python-handled exception.** A `try/except` without a `SAVEPOINT` is a Python-level lie — the Python exception is caught, but the underlying PostgreSQL transaction is poisoned, and every subsequent statement in the same transaction (including the eventual `conn.commit()`) fails with `current transaction is aborted, commands ignored until end of transaction block`.

#### Scenario: Operator follows code-derived analysis to find next blocker
- **WHEN** an operator hits `Store failed: column "X" of relation "Y" does not exist` in worker logs
- **THEN** the operator SHALL read `worker/stages/store.py` (or the equivalent failing-stage source file) end-to-end, SHALL extract every SQL statement and its referenced columns/tables, SHALL audit each reference against `information_schema`, and SHALL produce a gap list with each missing object mapped to the migration file that creates it

#### Scenario: Repair scope is bounded by the gap list
- **WHEN** the operator builds `repair.sql`
- **THEN** the file SHALL contain DDL only for migration files that create objects in the gap list, plus the corresponding `UPDATE schema_migrations` statements, and SHALL NOT include DDL from migrations whose objects do not appear in the gap list

#### Scenario: Hard blockers vs non-fatal gaps are classified by SAVEPOINT presence
- **WHEN** classifying gap-list entries
- **THEN** the operator SHALL mark a gap as HARD BLOCKER if the call site is NOT wrapped in `try/except`, OR if the call site is wrapped in `try/except` but is NOT wrapped in a `SAVEPOINT` that protects the parent transaction; the operator SHALL mark a gap as NON-FATAL only if BOTH conditions hold (`try/except` present AND `SAVEPOINT` present)

#### Scenario: A try/except without SAVEPOINT is a Python lie
- **WHEN** a helper function in a multi-statement transaction wraps a cursor call in `try/except` but no `SAVEPOINT` is held over that cursor call
- **THEN** any failure of that cursor call SHALL poison the parent transaction, every subsequent statement in the transaction SHALL be silently rejected with `current transaction is aborted`, and the eventual `conn.commit()` SHALL behave as an implicit `ROLLBACK`, rolling back ALL prior statements in the transaction including ones that ran successfully

## ADDED Requirements

### Requirement: Helper-call cursor blocks in shared transactions MUST be wrapped in SAVEPOINTs

Any cursor-using helper function called from inside a multi-statement transaction (where one connection is shared across multiple writes) MUST wrap its cursor block in a PostgreSQL `SAVEPOINT … RELEASE SAVEPOINT` if the helper is intended to be non-fatal on failure. The `SAVEPOINT` MUST be issued before the cursor work, `RELEASE SAVEPOINT` MUST be issued on the success path, and `ROLLBACK TO SAVEPOINT` MUST be issued on the exception path before the exception is re-raised or logged. Helpers that intentionally propagate failures (e.g., `_update_task_status`) MUST NOT be wrapped, because their failure should abort the entire transaction.

#### Scenario: Non-fatal helper survives a downstream failure
- **WHEN** a non-fatal helper inside Stage 5 Store wraps its cursor work in `SAVEPOINT sp_<name>` and the cursor work fails
- **THEN** the helper SHALL issue `ROLLBACK TO SAVEPOINT sp_<name>`, the helper SHALL log a "non-fatal" warning, the parent transaction SHALL remain in a non-aborted state, and every subsequent statement in the transaction (including `conn.commit()`) SHALL succeed if otherwise valid

#### Scenario: Savepoint name follows the sp_<function_name> pattern
- **WHEN** wrapping a helper in a SAVEPOINT
- **THEN** the savepoint name SHALL be `sp_<function_name>` with the leading underscore stripped from the function name (e.g., `_save_pattern` → `sp_save_pattern`); for inline blocks with no enclosing function, the name SHALL describe the operation (e.g., `sp_notify_task_completed`)

#### Scenario: SAVEPOINT helper is defensive against its own failures
- **WHEN** the `_savepoint` context manager itself encounters an error issuing `RELEASE SAVEPOINT` or `ROLLBACK TO SAVEPOINT` (because the parent transaction is already poisoned by an upstream bug not yet fixed)
- **THEN** the helper SHALL swallow the secondary error and SHALL still re-raise the original exception so the caller's `try/except` observes it

#### Scenario: Verification gate proves transaction is no longer poisoned
- **WHEN** verifying a savepoint repair on Stage 5 Store
- **THEN** the operator SHALL submit a real `brute_force` task, SHALL poll for ≤60s, SHALL confirm `status='completed'`, SHALL confirm worker logs contain the expected non-fatal warning lines (e.g., `Pattern save failed (non-fatal)`) but SHALL NOT contain any `current transaction is aborted, commands ignored until end of transaction block` lines and SHALL NOT contain any `Store failed:` line
