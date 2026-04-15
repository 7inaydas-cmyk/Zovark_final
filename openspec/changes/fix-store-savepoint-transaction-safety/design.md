## Context

`fix-store-stage-full-schema-gap` proved end-to-end that `store.py:_save_pattern`'s "non-fatal" warning is a Python lie. The full cascade (captured in that change's `section6_failure.log`):

1. `_store_investigation_body` opens one DB connection, runs everything in one transaction.
2. `_update_task_status` runs first → succeeds. The `agent_tasks` row is updated in the in-memory transaction state with `status='completed'`, `model_name='unknown'`, `verdict=...`.
3. `_save_pattern` runs next → `INSERT INTO investigation_memory (task_type, alert_signature, code_template, ...)` fails because the live `investigation_memory` table has a totally different shape (the documented schema fork).
4. `psycopg2` raises `psycopg2.errors.UndefinedColumn`.
5. The inner `try/except` at `store.py:218` catches the Python exception and prints `Pattern save failed (non-fatal): column "task_type" of relation "investigation_memory" does not exist`.
6. **PostgreSQL is now in `aborted transaction` state.** Per Postgres semantics, every subsequent statement in this transaction is silently rejected with `current transaction is aborted, commands ignored until end of transaction block` — until a `ROLLBACK`, a `ROLLBACK TO SAVEPOINT`, or end-of-transaction.
7. `_create_investigation` runs → rejected (its except prints "Investigation insert failed (non-fatal): current transaction is aborted").
8. `_insert_audit_event` for `investigation_completed` runs → rejected.
9. The inline `NOTIFY task_completed` runs → rejected.
10. `_update_dedup_entry` runs its inner SELECT → rejected, the inner `except: pass` swallows it, `alert_hash` stays None, the function takes the early-return path with `[STORE] WARNING: agent_tasks.dedup_hash missing`.
11. `conn.commit()` is called → in psycopg2, committing an aborted transaction does an **implicit ROLLBACK**. Step 2's UPDATE is rolled back along with everything else.
12. The task row is therefore never updated. It stays `pending`. Forever.

The Python `try/except` around `_save_pattern` (and around every other helper) was added with the intent that "if the optional save fails, the main investigation should still complete." That intent only holds if the failure is isolated from the parent transaction — which requires a `SAVEPOINT`.

Constraints from the user's spec:
- Only `worker/stages/store.py` may be modified.
- The `investigation_memory` schema fork must NOT be reconciled in this change (it stays a noise warning).
- `_save_pattern` must continue to write the same column list it writes today.
- No new migrations.
- The verification gate is identical to the previous change's §6: submit a brute_force task, poll for ≤60s, confirm `status='completed'`.

The user explicitly framed this change to be the **minimum code change that makes the existing "non-fatal" framing actually true**.

## Goals / Non-Goals

**Goals:**
- A failure inside any of the 5 wrapped helper call sites recovers the parent transaction and lets `_update_task_status`'s already-applied UPDATE survive `conn.commit()`.
- The verification gate (`brute_force` task → `status='completed'` within 60s) passes.
- Worker logs after the fix show:
  - `Started workflow from Redpanda`
  - `Pattern save failed (non-fatal): column "task_type" of relation "investigation_memory" does not exist` — still expected, the fork is out of scope
  - **NO** `current transaction is aborted, commands ignored until end of transaction block` lines for any call site downstream of `_save_pattern`
  - **NO** `Store failed:` line
- The savepoint helper is encapsulated in one `_savepoint(conn, name)` context manager so each call site is a single line of new code at the cursor work boundary.
- The same `_savepoint` helper handles its own internal error case (savepoint operation itself fails because the transaction is already poisoned by some upstream bug) without crashing the helper.

**Non-Goals:**
- Reconciling the `investigation_memory` schema fork. Out of scope; a separate change.
- Modifying `_update_task_status` or the `SET LOCAL app.current_tenant` block — they correctly propagate failures.
- Adding savepoints to other pipeline stages (`assess.py`, `analyze.py`, etc.). Out of scope; their cursor patterns may be different and should be audited per the next-stage's own change.
- Refactoring `store.py` for any other reason (style, naming, helper extraction, type hints, logging). Strictly the savepoint additions.
- Changing what columns `_save_pattern` writes to. The spec forbids it. The whole point is that even with the wrong columns, Stage 5 should still complete.
- Adding tests. The verification gate (live task submission + poll + DB check) is the test for this change. The repo already has 535 unit tests; adding a unit test for SAVEPOINT semantics would require a real PG fixture, which is more apparatus than the change deserves.
- Touching `_update_dedup_entry`'s outer try/except — only the inner SELECT block needs the savepoint.

## Decisions

### Decision 1: Use a `_savepoint(conn, name)` context manager, not inline SAVEPOINT/RELEASE everywhere

**Choice**: Define one new helper:

```python
@contextmanager
def _savepoint(conn, name):
    """Wrap a block in a Postgres SAVEPOINT so cursor failures inside the block
    do not poison the parent transaction. On exception, ROLLBACK TO SAVEPOINT and
    re-raise so the surrounding try/except still observes the Python exception."""
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        except Exception:
            pass
        raise
    else:
        try:
            with conn.cursor() as cur:
                cur.execute(f"RELEASE SAVEPOINT {name}")
        except Exception:
            pass
```

Then each helper call site becomes one extra line:

```python
def _save_pattern(conn, ...):
    try:
        with _savepoint(conn, "sp_save_pattern"):
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO investigation_memory ...""", (...))
    except Exception as e:
        print(f"Pattern save failed (non-fatal): {e}")
```

**Alternatives considered**:
- *Inline `cur.execute("SAVEPOINT ...")` … `cur.execute("RELEASE ...")` in every helper.* Rejected: 5× the boilerplate, each helper ends up with 4 levels of nested try/except, very easy to get the rollback path wrong on copy-paste.
- *Wrap each helper at the call site (in the main body) instead of inside the helper.* Rejected: the Python `try/except` is *inside* the helper, so a savepoint on the outside cannot catch the swallowed exception. We'd have to remove the inner try/except, push it to the call site, and the call sites are already crowded.
- *Use psycopg2's `with conn:` block around each call.* Rejected: `with conn:` commits/rollbacks the entire transaction, not a savepoint. Wrong scope.

**Rationale**: One reusable helper, one extra `with` line per call site, identical surrounding code shape, single-source-of-truth for the savepoint protocol. Encapsulates the "rollback even if savepoint operations themselves fail" defensive paths in one place.

### Decision 2: The savepoint name pattern is `sp_<function_name>` with the leading underscore stripped

**Choice**:
- `_save_pattern` → `sp_save_pattern`
- `_create_investigation` → `sp_create_investigation`
- `_insert_audit_event` → `sp_insert_audit_event`
- `_update_dedup_entry` → `sp_update_dedup_entry`
- inline NOTIFY → `sp_notify_task_completed`

**Alternatives considered**:
- *Preserve the leading underscore: `sp__save_pattern`*. Postgres allows it but double underscore looks like a typo and is harder to read in logs.
- *Drop the `sp_` prefix*. The user spec mandates it.

**Rationale**: The user's spec says "`sp_<function_name>`". The underscore-stripping is a small readability concession that doesn't break that contract. The pattern is unique per call site (the inline NOTIFY is named after its NOTIFY channel, since there's no enclosing function).

### Decision 3: `_insert_audit_event` reuses the same savepoint name across both call sites

**Choice**: Both `_insert_audit_event` calls (the `investigation_started` audit at the top of the body and the `investigation_completed` audit near the end) use `sp_insert_audit_event`.

**Rationale**: Postgres scopes savepoint identifiers per `SAVEPOINT` execution. When a second `SAVEPOINT sp_insert_audit_event` runs while the first is still active, Postgres replaces (effectively shadows) the first with the second, and `RELEASE` / `ROLLBACK TO` operates on the most recent. In our flow, the first `_insert_audit_event` always issues `RELEASE` before the second one runs, so there's no shadowing in practice — they're sequential. Reusing the name is correct and avoids tracking two near-identical names.

### Decision 4: `_update_task_status` does NOT get a savepoint

**Choice**: Leave `_update_task_status` unchanged.

**Rationale**: It has no surrounding `try/except`. Any failure there propagates to the outer body's except clause at `store.py:450`, which calls `conn.rollback()` and sets `status = "failed"`. That is the correct behavior — if the main task UPDATE fails, the entire investigation must be marked failed, not papered over with a savepoint rollback. Adding a savepoint here would change observable behavior and is outside scope.

### Decision 5: `_savepoint`'s ROLLBACK arm re-raises after rolling back

**Choice**: After the rollback, the context manager re-raises the original exception so the caller's existing `try/except` still observes it and logs the warning.

**Alternatives considered**:
- *Swallow the exception inside `_savepoint`*. Rejected: removes the warning log, hides the schema fork, makes future debugging harder.

**Rationale**: The contract is "make existing non-fatal handlers actually non-fatal at the Postgres level." That means the Python error path (warn + continue) must still execute. Re-raising preserves that.

### Decision 6: Verify with a real pipeline submission, not a unit test

**Choice**: Same verification recipe as the previous two changes — login, submit `brute_force`, poll for ≤60s, confirm `status='completed'`, confirm worker logs are clean of "transaction is aborted" lines.

**Rationale**: The bug only manifests against a real PostgreSQL transaction with a real failing INSERT inside a real connection. Unit-testing SAVEPOINT semantics requires either a live PG fixture (overkill) or a mock that fakes psycopg2's transaction state machine (fragile). The repo's pattern for Stage 5 verification is already a live submission. Match the pattern.

## Risks / Trade-offs

- **[Risk] The savepoint helper's own SAVEPOINT statement could fail if the parent transaction is already poisoned by some upstream bug we haven't found yet** → **Mitigation**: the helper is defensive — the inner `try/except` around `RELEASE` and `ROLLBACK TO` swallows secondary failures and the outer `raise` still surfaces the original Python exception. In the worst case we get the same behavior as today (transaction stays poisoned, nothing recovers) plus a clearer call stack.
- **[Risk] Savepoints across multiple cursors on the same connection** → **Mitigation**: psycopg2's `conn.cursor()` returns lightweight cursor objects that share the same underlying transaction. SAVEPOINT visibility is per-transaction, not per-cursor. Issuing the SAVEPOINT in one `with conn.cursor()` block and then doing the actual work in a different `with conn.cursor()` block is fine; both see the same transaction state.
- **[Risk] An f-string in the SAVEPOINT name** → **Mitigation**: every name is a hardcoded string from a closed set defined in this file. No user input. No SQL injection surface.
- **[Trade-off] We do not refactor the helper functions to take a single-cursor-per-call** → That would be a cleaner API but a bigger diff. Not in scope.
- **[Trade-off] We do not add a global savepoint to `_update_task_status`** → Out of scope. If the main UPDATE fails, the task should fail. That's correct.
- **[Risk] Future helper functions added to `store.py` may forget to use `_savepoint`** → **Mitigation**: the new spec requirement on `schema-migration-integrity` (revised classification rule) makes this explicit. Future code reviews on `store.py` should check that any new cursor-call helper either propagates failures (no try/except) or wraps its body in `_savepoint`.

## Migration Plan

This is a code-only change; "migration" here means "deploy steps":

1. Edit `worker/stages/store.py`:
   - Add `_savepoint` context manager near `_db_conn`.
   - Wrap `_update_dedup_entry`'s inner SELECT block.
   - Wrap `_save_pattern`'s INSERT.
   - Wrap `_create_investigation`'s INSERT.
   - Wrap `_insert_audit_event`'s INSERT.
   - Wrap the inline NOTIFY block.
2. Rebuild and restart the worker container: `docker compose build worker && docker compose up -d worker`.
3. Verify the worker is healthy: `docker ps | grep worker_mine-worker`.
4. Run the verification recipe (login, submit `brute_force`, poll).
5. If verification passes, capture post-state evidence.

**Rollback strategy**: revert the diff with `git checkout worker/stages/store.py`. Rebuild worker. Behavior returns to the previous (broken) state. No DB state is touched, so rollback is instant and reversible.

## Open Questions

- Should the `investigation_memory` schema fork be reconciled in the next change? **Open**: yes, but as a separate change. Now that the savepoint fix unblocks Stage 5, the fork is a noise issue, not a correctness issue, so the urgency is lower.
- Should `assess.py`, `analyze.py`, `execute.py`, and `govern.py` be audited for the same Python-vs-Postgres `try/except` mismatch? **Open**: probably yes. Each stage has its own connection scope and its own helpers, so each needs an independent audit. Future change.
- Should `_savepoint` be moved to a shared `worker/db_helpers.py` so other stages can use the same primitive? **Open**: not in this change (constraint: only modify `store.py`). Worth doing later when the next stage needs it.
