## Why

`worker/stages/store.py:_store_investigation_body` runs every Stage 5 write inside a single PostgreSQL transaction. Five helper call sites (`_update_dedup_entry`, `_save_pattern`, `_create_investigation`, `_insert_audit_event`, and an inline `NOTIFY task_completed` block) wrap their cursor work in Python `try/except` and print "non-fatal" warnings on failure. Those handlers are non-fatal at the **Python** level but fatal at the **PostgreSQL** level: once any statement in a transaction throws, every subsequent statement is rejected with `current transaction is aborted, commands ignored until end of transaction block`, and the eventual `conn.commit()` becomes an implicit `ROLLBACK`. The previous change (`fix-store-stage-full-schema-gap`) proved this end-to-end: after the 047 ledger repair, `_save_pattern` hit the documented `investigation_memory` schema fork, the warning logged, and then **everything else in Stage 5 silently rolled back, including the `_update_task_status` UPDATE that had already run successfully.** Tasks stay `pending` forever.

The fix is to wrap each helper-call cursor block in a `SAVEPOINT … RELEASE SAVEPOINT` (with `ROLLBACK TO SAVEPOINT` in the except branch) so that recoverable failures truly are recoverable. This is a pure code change — no schema changes, no migrations, no other files touched.

## What Changes

- Add a `_savepoint(conn, name)` context manager near the existing `_db_conn` helper in `worker/stages/store.py`. It issues `SAVEPOINT <name>` on entry, `RELEASE SAVEPOINT <name>` on clean exit, and `ROLLBACK TO SAVEPOINT <name>` on exception (then re-raises so the surrounding Python `try/except` still logs its warning).
- Wrap the cursor work in five call sites with this context manager. Each gets a unique savepoint name following the pattern `sp_<function_name>` (with the function's leading underscore dropped for readability):
  - `_save_pattern` → `sp_save_pattern`
  - `_create_investigation` → `sp_create_investigation`
  - `_insert_audit_event` → `sp_insert_audit_event` (the same name is reused for both call sites in the main body — Postgres scopes savepoint identifiers per execution, so this is safe)
  - `_update_dedup_entry` (inner SELECT) → `sp_update_dedup_entry`
  - inline NOTIFY block → `sp_notify_task_completed`
- **Do NOT** modify `_update_task_status` — it has no try/except wrapper because its failure SHOULD abort the investigation (no savepoint needed; the existing `conn.rollback()` in the outer except handles it correctly).
- **Do NOT** modify `SET LOCAL app.current_tenant` — it has early-return error handling that aborts the investigation entirely, which is the correct behavior for a tenant validation failure.

## Capabilities

### Modified Capabilities
- `schema-migration-integrity`: revise the existing classification rule. The previous spec said a gap is non-fatal if the call site is in `try/except`. That is necessary but not sufficient. **A gap is non-fatal only if (a) the call site is in `try/except` AND (b) the call site is wrapped in a `SAVEPOINT` so the surrounding transaction survives.** Without (b), `try/except` is a Python-level lie that conceals Postgres-level transaction poisoning.

## Impact

- **Code**: one file modified, `worker/stages/store.py`. No other file is touched.
- **Behavior**: Stage 5 Store gains real fault-isolation between helper writes. The existing `investigation_memory` schema fork stops blocking Stage 5; the warning still logs, but the rest of the transaction completes and the task transitions to `completed`.
- **Schema**: zero changes. No migrations, no new tables, no new columns.
- **Risk**: low. SAVEPOINTs are a well-understood Postgres primitive. The added scope is bounded — each savepoint covers exactly the cursor work of one helper. If a savepoint operation itself fails (e.g., the savepoint helper hits an aborted-transaction state), the inner try/except in the helper still catches and logs.
- **Performance**: negligible. Each SAVEPOINT/RELEASE pair is a single-statement-per-savepoint operation against the active transaction; no disk IO, no locks beyond what the helper already takes.
- **Reversibility**: trivial — revert the diff. The rest of the system is unaware of the change.
- **Operators**: this fix is the prerequisite for declaring Stage 5 healthy on the dev volume. Combined with the previous two changes (040, 046, 047), Stage 5 will finally complete end-to-end on a fresh task.
- **Out of scope**: reconciling the `investigation_memory` schema fork itself. That remains a code-vs-DB decision for a separate change. With the savepoint fix in place, the fork becomes a noise issue (a recurring "Pattern save failed" warning) rather than a correctness issue.
