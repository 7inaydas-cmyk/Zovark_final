## Context

`fix-store-stage-schema-blocker` repaired migrations 040 and 046 cleanly. Sections 1–4 of that change all passed. Section 5 (pipeline submission) failed when the worker hit a *new* missing column on the same `_update_task_status` UPDATE statement: `Store failed: column "model_name" of relation "agent_tasks" does not exist`. The previous change halted per its rollback plan and deferred the next step to operator decision. This change is that next step.

The user's mandate this time is sharper: don't guess at the next blocker, *prove* the gap from `store.py` itself. Phase A executed that proof in advance:

1. Read `worker/stages/store.py` end-to-end (476 lines).
2. Extracted every SQL statement: 1 SELECT (`agent_tasks.dedup_hash`), 1 UPDATE (`agent_tasks` — 13 columns), 3 INSERTs (`investigation_memory`, `investigations`, `audit_events`), 1 NOTIFY.
3. Ran a single SQL audit against `information_schema.columns` for every table.column referenced by those statements (32 column-presence checks, 4 table-existence checks).
4. Cross-referenced each missing object against `migrations/`.
5. Classified each gap by whether `store.py` wraps the call site in `try/except` (non-fatal) or not (hard blocker).

The result is a tight gap list of exactly **one migration file** that fixes the only hard blocker (and incidentally the one non-fatal mismatch in the same neighborhood). Every other gap is either a code/schema fork (`investigation_memory`) or unrelated to `store.py` entirely.

Constraints carried over from the previous change:
- Cannot wipe the dev volume.
- `scripts/apply_migrations.sh` cannot fix this on its own — the lying `init_sql` row hides 047 from the runner just as it hid 040 and 046.
- Repair must use `manual_backfill` for the audit trail.
- DDL must be idempotent (`IF NOT EXISTS`).
- The 040/046 repair stands and is **not** touched.

New constraints from the user this round:
- "Only fix what `store.py` actually references."
- "Do not modify `store.py`. The code is correct; the DB is behind."
- "Do not touch token_quotas or monthly_tokens_used."
- "If Phase A reveals more than 6 missing migrations, stop after Phase A and report before building repair.sql." → Phase A revealed 1; safely under the gate.

## Goals / Non-Goals

**Goals:**
- Stage 5 Store completes successfully for new investigations within one task submission of the fix.
- `agent_tasks.model_name` and `investigations.model_name` exist with `DEFAULT 'unknown'`.
- Both supporting indexes exist.
- The `schema_migrations` row for `047_add_model_name.sql` reflects reality (`manual_backfill`).
- The Phase A methodology is encoded in the new spec requirement so it becomes the canonical way to handle future ledger-drift discoveries.

**Non-Goals:**
- Reconciling `investigation_memory`'s schema fork. That requires deciding whether to (a) modify `store.py` to write the live shape, (b) drop+recreate the table per migration 045, or (c) leave the non-fatal warning. Each option has different blast radii and is a separate change.
- Touching the other 11 lying-ledger rows from the 13-row suspect list. None of them block `store.py`.
- Modifying `apply_migrations.sh` to detect physical drift.
- Retroactively completing the now-stuck `b184a51d-…` task from the previous change. It is dead in Temporal and will be cleaned up via normal stale-workflow termination.
- Writing a comprehensive ledger-vs-physical schema diff tool. Worth doing, separate change.

## Decisions

### Decision 1: Apply only migration 047, not the broader 13-row list

**Choice**: Repair exactly `047_add_model_name.sql` and leave the other 11 suspect rows untouched.

**Alternatives considered**:
- *Apply all 13 lying-ledger rows in one shot.* Rejected: large blast radius (potentially 50+ DDL statements across ~13 files), no guarantee they're individually idempotent, and most of those migrations create objects that no current code references — so they'd add noise without fixing anything user-visible.
- *Apply 047 plus a hand-picked subset of "probably needed" rows.* Rejected: speculative. Phase A is the proof; any migration outside the proof is a guess.

**Rationale**: The Phase A audit is the discriminator. If `store.py` doesn't reference an object, applying the migration that creates it cannot affect Stage 5. Tight scope = small repair = small rollback surface = clear audit trail.

### Decision 2: Leave the `investigation_memory` 6-column gap as a documented non-fatal warning

**Choice**: Do not invent new columns on the live `investigation_memory` table to satisfy `store.py:_save_pattern`.

**Alternatives considered**:
- *`ALTER TABLE investigation_memory ADD COLUMN task_type ...` (×6).* Rejected: would create a hybrid schema that exists in neither `init.sql` nor any migration file. Future operators reading either source would see a table that doesn't match what they expect. Also violates the spec rule "do not apply migrations for objects store.py never touches" interpreted in reverse: do not invent DDL that isn't in any migration.
- *Drop the live `investigation_memory` table and apply migration 045 to recreate it.* Rejected: destroys data on a 6-hour-old volume, breaks any code that reads from the live shape (search_pattern, embedding similarity), and the spec forbids modifying `store.py`.
- *Patch `store.py:_save_pattern` to write the live shape.* Rejected: spec explicitly forbids modifying `store.py`.

**Rationale**: The `_save_pattern` function is wrapped in `try/except` and prints `Pattern save failed (non-fatal)` on failure. Production behavior with the gap is identical to production behavior without the fix from `_save_pattern`'s perspective: the warning is logged, the rest of Stage 5 continues, the task transitions to `completed`. The mismatch is real but is a strictly different problem (code/DB fork) requiring a different kind of decision than "apply the missing migration".

### Decision 3: Single-transaction repair, identical pattern to the previous change

**Choice**: BEGIN; ALTER TABLE; ALTER TABLE; CREATE INDEX; CREATE INDEX; UPDATE schema_migrations; COMMIT — all in one psql heredoc.

**Rationale**: Identical to `fix-store-stage-schema-blocker`'s Decision 1 and 2. Reusing the exact same pattern keeps the audit trail consistent (`source='manual_backfill'`), avoids reinventing the wheel, and means any future operator reading either change can reuse the recipe.

### Decision 4: Add a dry-run-via-ROLLBACK gate before the real apply

**Choice**: Run the entire repair SQL with `BEGIN; <DDL>; ROLLBACK;` first, capture the output, then run the real `BEGIN; <DDL>; COMMIT;`.

**Alternatives considered**:
- *Skip the dry run* (as the previous change did). The previous change went straight from `repair.sql` to `psql < repair.sql`. It worked, but only because the DDL happened to be clean. A dry run is cheap insurance.

**Rationale**: The user's spec explicitly asks for a dry run (Phase B step 3). It also makes the recipe safer for the next operator: any syntax error or constraint violation surfaces in a transaction that gets rolled back, leaving the schema untouched.

### Decision 5: The new spec requirement encodes "gap analysis from code, not from rumor"

**Choice**: Add a new requirement under `schema-migration-integrity` that any future ledger-drift repair MUST extract its target object list from the consuming code's SQL statements, not from a hand-maintained list of "missing migrations".

**Rationale**: The previous change knew about migration 040 and 046 because `_update_task_status`'s error message named them. It did NOT know about 047 because that error was masked by the earlier missing-column error. Phase A's grep-store.py methodology surfaces the *complete* hard-blocker set in a single pass, which is why this change's scope is provably tight. Encoding the methodology in a spec requirement makes future changes inherit the discipline.

## Risks / Trade-offs

- **[Risk] Phase A missed an SQL statement in `store.py`** → **Mitigation**: I read all 476 lines and extracted the SQL by direct quotation, not by pattern-match. The new spec requirement also asks the next operator to re-run the extraction whenever store.py changes. Section 5 of the new tasks is the ground-truth check: a real submission must complete; if it doesn't, we know Phase A was incomplete.
- **[Risk] A non-`store.py` consumer (e.g., `assess.py`, `analyze.py`) writes to `agent_tasks` or `investigations` and hits its own missing column** → **Mitigation**: out of scope for this change. Stage 5 Store is the only stage we're targeting. If a different stage breaks, that's a different change, and the same gap-analysis methodology applies to that stage's source file.
- **[Risk] The `investigation_memory` non-fatal warning fills the worker logs with noise** → **Mitigation**: it already does, and has for months. This change does not improve the noise floor but also does not worsen it. A follow-up change should reconcile the fork.
- **[Trade-off] We are NOT building an automated drift detector** → A `--verify-physical` flag on `apply_migrations.sh` would catch this class of bug pre-emptively. Doing so here would balloon scope. Worth doing later.
- **[Risk] Re-running `init.sql` (e.g., after `docker compose down -v`) re-introduces the drift** → **Mitigation**: documented; same risk as the previous change. Long-term fix is regenerating `init.sql` to match migration history.

## Migration Plan

1. **Pre-flight**: snapshot the 047 ledger row and current absence of `model_name` columns to `pre_state.txt`.
2. **Phase A re-confirmation in the live DB**: re-run the column-presence audit and confirm `agent_tasks.model_name=f, investigations.model_name=f`. (We already did this in the propose step; this re-run guards against any concurrent state change between propose and apply.)
3. **Compose `repair.sql`** with the verbatim DDL from `migrations/047_add_model_name.sql` plus the single `UPDATE schema_migrations` statement, all wrapped in a single `BEGIN; … COMMIT;`.
4. **Dry run**: pipe a temporary version of `repair.sql` with `ROLLBACK` instead of `COMMIT` into psql; confirm the output contains all 4 DDL OK lines and no `ERROR:` lines.
5. **Real apply**: `docker exec -i zovark-postgres psql -U zovark -d zovark < repair.sql 2>&1 | tee repair.out`. Confirm output ends with `COMMIT` and contains `UPDATE 1`.
6. **Schema verification gate**:
   ```sql
   SELECT model_name FROM agent_tasks LIMIT 1;
   SELECT model_name FROM investigations LIMIT 1;
   SELECT indexname FROM pg_indexes WHERE indexname IN ('idx_agent_tasks_model','idx_investigations_model');
   SELECT filename, source, applied_at FROM schema_migrations WHERE filename='047_add_model_name.sql';
   ```
   All four must succeed; ledger row must show `manual_backfill`.
7. **Pipeline verification gate**: submit one `brute_force` task, poll for up to 60s, confirm `status='completed'`, confirm worker log contains no new `Store failed` lines for the new task_id.
8. **Snapshot post-state** to `post_state.txt`.

**Rollback strategy**: identical to the previous change. The new columns and indexes are additive and idempotent; removing them is safe but unnecessary. The ledger UPDATE is a single-row reversible statement.

## Open Questions

- Should the next change reconcile `investigation_memory`'s code/schema fork? **Open**: yes, but as a separate change requiring a code-vs-DB decision (modify store.py vs. drop+recreate the table vs. accept the warning).
- Should `apply_migrations.sh` grow `--verify-physical`? **Still open** from the previous change.
- Is there a similar lying-ledger pattern hiding in `assess.py`, `analyze.py`, `execute.py`, `govern.py`, or `ingest.py`? **Open**: future changes should run the same Phase A audit on each stage's source file.
