## Context

The Zovark dev volume has been live since at least 2026-01-01 with `init.sql` as bootstrap. At some point a back-fill migration (`072_schema_migrations_ledger.sql` and/or related plumbing) inserted `init_sql` rows for migrations 000–053 into `schema_migrations`, asserting that those files had already been applied as part of bootstrap. **They had not.** `init.sql` only contains a static, hand-written subset of the early schema — it was never regenerated to reflect every numbered migration. As a result the migration runner (`scripts/apply_migrations.sh`) skips files whose ledger row exists, and the actual physical schema has been silently behind for months.

The acute symptom: `worker/stages/store.py` writes `needs_human_review = true` and inserts into `llm_audit_log` on every Path C and many Path A investigations. Both DDLs live in migrations 040 and 046, both of which the ledger falsely marks `init_sql`. Every `InvestigationWorkflowV2` in dev has been silently failing in Stage 5 since the false rows were inserted. Worker logs from the past several hours show ≥7 distinct task_ids hitting the same `Store failed` error, and `agent_tasks.status` for our test task `c24edf30-20be-41d3-936b-5080cd6bb970` is permanently stuck at `pending`.

Constraints:
- Cannot wipe the volume — it holds 3+ months of investigation history, audit events, and seeded fixtures.
- `scripts/apply_migrations.sh` is ledger-aware and idempotent but trusts the ledger; it cannot detect physical drift on its own.
- All target DDLs in 040 and 046 use `IF NOT EXISTS`, so reapplication is naturally safe.
- The `schema_migrations.source` column has a CHECK constraint allowing only `init_sql`, `migration_runner`, `manual_backfill`. We need `manual_backfill` for repaired rows so the audit trail distinguishes real reapplications from suspect bootstrap claims.
- Must not block on the broader 15-migration audit. That is follow-up work.

Stakeholders: dev/SOC operators currently unable to demo the pipeline; CI which submits e2e probes; future operators reading `docs/RUNBOOK_HEALTHCHECK.md#schema-drift`.

## Goals / Non-Goals

**Goals:**
- Stage 5 Store completes successfully for new investigations within one task submission of the fix.
- The two target DDL objects (`agent_tasks.needs_human_review`, `agent_tasks.review_reason`, `idx_tasks_human_review`, `llm_audit_log` table + 4 indexes) exist in the live schema.
- `schema_migrations` rows for `040_human_review_flags.sql` and `046_llm_audit_log.sql` reflect reality: `source='manual_backfill'`, `applied_at=now()`, `applied_by=CURRENT_USER`.
- Repair is reproducible from a documented recipe so the next operator who hits ledger drift on a different migration can follow the same steps without re-deriving them.
- All work is gated through `scripts/apply_migrations.sh --dry-run` first, then real apply.

**Non-Goals:**
- Auditing or repairing the other ~13 suspect `init_sql`-marked migrations. Each requires its own grep + diff + apply cycle and may have ordering dependencies.
- Adding `monthly_tokens_used` to `token_quotas`. The column does not exist in any migration; the actual column is `tokens_used`. If a Go or Python caller references `monthly_tokens_used`, that is a code bug, not a missing migration, and belongs in a separate change.
- Rebuilding `init.sql` to match the full migration history. Long-term cleanup, separate change.
- Retroactively completing the ≥7 already-stuck `pending` tasks. Those are dead workflows in Temporal; they will be cleaned up as part of normal stale-workflow termination.
- Modifying `apply_migrations.sh` to detect physical drift. Worth doing eventually but out of scope here.

## Decisions

### Decision 1: Repair via direct SQL, not a new numbered migration

**Choice**: Apply the contents of `040_human_review_flags.sql` and `046_llm_audit_log.sql` by piping them straight into `psql` as the `zovark` superuser, then `UPDATE schema_migrations` to mark the two rows `manual_backfill`.

**Alternatives considered**:
- *Create a new migration `073_repair_040_046.sql` that re-runs the DDL.* Rejected: pollutes the migration history with a file that is logically a no-op everywhere except this one drifted volume, and the ledger row would then claim the *new* file ran rather than recording that the *original* was reapplied. Loses audit fidelity.
- *Delete the false ledger rows and let `apply_migrations.sh` reapply on the next run.* Rejected for two reasons: (1) the runner only iterates files in lexical order and may try to apply intermediate migrations we have not audited, widening blast radius; (2) leaves a window where the ledger is empty and another caller could race a partial apply.

**Rationale**: Direct SQL is the smallest possible change. Both DDLs are idempotent (`IF NOT EXISTS` everywhere), and the manual `UPDATE` to the ledger is a single transaction touching exactly two rows. The audit trail clearly says "manually backfilled on 2026-04-13" rather than pretending a fresh migration shipped.

### Decision 2: Use `source='manual_backfill'`, not `migration_runner`

**Choice**: When repairing the two ledger rows, set `source='manual_backfill'`.

**Alternatives considered**:
- *`migration_runner`*: implies the runner script chose to apply this file, which it did not.
- *`init_sql`*: the lie we are correcting — re-using it defeats the purpose of the audit.

**Rationale**: The CHECK constraint already exposes `manual_backfill` as the intended escape hatch for cases like this. Future operators querying `WHERE source = 'manual_backfill'` get an immediate list of all drift repairs.

### Decision 3: Verify with a real pipeline submission, not just `\d`

**Choice**: After repair, submit one `brute_force` task via the API and poll for `status='completed'`. Treat the change as failed if the task does not complete.

**Alternatives considered**:
- *Stop at `\d agent_tasks` and `SELECT 1 FROM llm_audit_log`.* Rejected: schema presence is necessary but not sufficient. Stage 5 Store touches several other tables and could fail elsewhere; a real submission is the only ground truth.

**Rationale**: We already know how to reproduce the failure from the conversation. Reproducing the success path is one curl + one poll loop and gives a definitive answer.

### Decision 4: Apply 040 first, then 046

**Choice**: Lexical / numeric order.

**Rationale**: Neither migration depends on the other (040 ALTERs `agent_tasks` which already exists from `init.sql`; 046 CREATEs an unrelated `llm_audit_log` table with no FKs to `agent_tasks`), so order is mechanically irrelevant. Going in numeric order matches operator muscle memory and keeps the recipe reusable.

## Risks / Trade-offs

- **[Risk] The 13 other suspect `init_sql` rows hide further blockers** → **Mitigation**: out of scope here, but the verification step (real pipeline submission) will surface any other Store-stage failures immediately. Follow-up change should run a comprehensive ledger-vs-physical-schema diff.
- **[Risk] A future `docker compose down -v` followed by `up -d` will re-run `init.sql` and re-introduce the drift** → **Mitigation**: documented in design.md and tasks.md as known-not-fixed; fix belongs to "regenerate init.sql or remove it" which is a larger structural change. CLAUDE.md Known Issue #13 already warns about volume-fresh schema drift.
- **[Risk] `manual_backfill` rows look noisy in the audit trail** → **Mitigation**: that is the *point*. Two rows is a small price for honesty about what ran and why.
- **[Trade-off] We do not modify `apply_migrations.sh` to validate physical schema before trusting the ledger** → A more robust runner would. Doing so here would balloon scope; the current change is a one-shot data fix, not a tooling change.
- **[Risk] Other workers / API processes might be mid-write to `agent_tasks` when we ALTER it** → **Mitigation**: `ALTER TABLE … ADD COLUMN IF NOT EXISTS` with a default of `FALSE` takes a brief `ACCESS EXCLUSIVE` lock but completes in milliseconds on dev volume size. We will run during the existing pipeline-stuck window where no Store-stage writes are succeeding anyway.

## Migration Plan

1. **Pre-flight**: snapshot the two relevant ledger rows (`SELECT * FROM schema_migrations WHERE filename IN ('040_human_review_flags.sql','046_llm_audit_log.sql')`) into the change directory as `pre_state.txt` for rollback evidence.
2. **Dry run**: `scripts/apply_migrations.sh --dry-run` and confirm 040 and 046 are *not* in the dry-run apply set (they will not be — the false ledger rows will hide them). This proves we cannot fix this through the runner alone and motivates the manual repair.
3. **Repair, in a single transaction**:
   ```sql
   BEGIN;
   -- 040
   ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS needs_human_review BOOLEAN DEFAULT FALSE;
   ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS review_reason TEXT;
   CREATE INDEX IF NOT EXISTS idx_tasks_human_review
       ON agent_tasks(needs_human_review) WHERE needs_human_review = TRUE;
   UPDATE schema_migrations
       SET source='manual_backfill', applied_at=now(), applied_by=CURRENT_USER
       WHERE filename='040_human_review_flags.sql';
   -- 046
   CREATE TABLE IF NOT EXISTS llm_audit_log (... -- exact body from migrations/046_llm_audit_log.sql);
   CREATE INDEX IF NOT EXISTS idx_llm_audit_task    ON llm_audit_log(task_id);
   CREATE INDEX IF NOT EXISTS idx_llm_audit_tenant  ON llm_audit_log(tenant_id);
   CREATE INDEX IF NOT EXISTS idx_llm_audit_created ON llm_audit_log(created_at);
   CREATE INDEX IF NOT EXISTS idx_llm_audit_model   ON llm_audit_log(model_name);
   UPDATE schema_migrations
       SET source='manual_backfill', applied_at=now(), applied_by=CURRENT_USER
       WHERE filename='046_llm_audit_log.sql';
   COMMIT;
   ```
4. **Verify schema**:
   ```sql
   SELECT needs_human_review, review_reason FROM agent_tasks LIMIT 1;
   SELECT 1 FROM llm_audit_log LIMIT 0;
   SELECT filename, source, applied_at FROM schema_migrations
       WHERE filename IN ('040_human_review_flags.sql','046_llm_audit_log.sql');
   ```
5. **Verify pipeline**: submit one `brute_force` task via the existing curl recipe from CLAUDE.md, poll until `status='completed'`, and confirm worker logs no longer print `Store failed`.
6. **Snapshot post-state** to `post_state.txt` in the change directory.

**Rollback strategy**: if Step 5 fails, the new columns and table are harmless (no caller will be confused by their presence). The ledger UPDATE is reversible: `UPDATE schema_migrations SET source='init_sql', applied_at='2026-01-01', applied_by='zovark' WHERE filename IN (...)`. We do not need to drop the columns — they are additive and idempotent.

## Open Questions

- Should we audit the remaining ~13 suspect `init_sql` rows in this change or defer to a follow-up? **Resolved**: defer, per proposal scope.
- Does any application code expect `token_quotas.monthly_tokens_used`? **Open**: needs a `grep` across the Go API and Python worker. If yes, separate bug; if no, the user's "if in same range" hedge can be retired.
- Should `apply_migrations.sh` grow a `--verify-physical` flag that compares ledger entries against `pg_class` and warns on mismatches? **Open**: tooling improvement, separate change.
