## Why

Stage 5 (Store) of `InvestigationWorkflowV2` crashes on every recent task with `column "needs_human_review" of relation "agent_tasks" does not exist` and `relation "llm_audit_log" does not exist`. The pipeline runs through ingest → analyze → execute → assess → govern correctly, but the final write fails, so `agent_tasks` rows never leave `status='pending'` and the dashboard sees zero completions. The root cause is **ledger drift**: `schema_migrations` claims migrations 040 (`needs_human_review`/`review_reason`) and 046 (`llm_audit_log`) were applied via `init_sql` on 2026-01-01, but `init.sql` never actually contained those statements — the bootstrap created a stale subset of the schema, and a back-fill INSERT marked the missing migrations as already-applied. The migration runner therefore skips them. This blocks all investigation completion in dev.

## What Changes

- Apply migration `040_human_review_flags.sql` to add `agent_tasks.needs_human_review` (bool) + `agent_tasks.review_reason` (text) + `idx_tasks_human_review`.
- Apply migration `046_llm_audit_log.sql` to create `llm_audit_log` table + 4 indexes.
- Repair `schema_migrations` ledger so the `init_sql`-marked rows reflect what `init.sql` truly creates. Specifically: delete the false `init_sql` row for any migration whose objects do not exist in the live schema, and reinsert with `source='manual_backfill'` after the migration is actually applied.
- Add a verification step: `SELECT needs_human_review FROM agent_tasks LIMIT 1;` and `SELECT 1 FROM llm_audit_log LIMIT 0;` must succeed; submit one `brute_force` task and confirm it reaches `status='completed'`.
- **Out of scope**: `token_quotas.monthly_tokens_used` — the column does not exist in any migration file (`028_token_quotas.sql` defines `tokens_used`, not `monthly_tokens_used`). If a caller expects that name, that is a separate bug requiring a code-vs-schema decision, not a missing migration.
- **Out of scope**: blanket reapplication of all 15 confirmed-missing migrations. Each migration must be individually grepped for its objects, checked against live schema, and applied only if its objects are absent — to avoid destructive DDL or dependency ordering surprises.

## Capabilities

### New Capabilities
- `schema-migration-integrity`: rules governing the truthfulness of `schema_migrations` ledger, the repair procedure when the ledger drifts from physical schema, and the verification gate that Stage 5 Store requires.

### Modified Capabilities
<!-- none — no existing specs in openspec/specs/ -->

## Impact

- **Code**: none. This is a data fix on the running PostgreSQL instance plus a one-shot ledger repair. No application code changes.
- **DB objects added**: 2 columns on `agent_tasks`, 1 partial index on `agent_tasks`, 1 table `llm_audit_log` with 4 indexes.
- **DB ledger rows touched**: at minimum the `init_sql` rows for `040_human_review_flags.sql` and `046_llm_audit_log.sql` will be rewritten. Other false `init_sql` rows are flagged for follow-up but not touched in this change.
- **Pipeline**: unblocks Stage 5 Store. Tasks currently stuck in `pending` (e.g. `c24edf30-20be-41d3-936b-5080cd6bb970`, `5172044d-…`, `4420a1ef-…` — at least 7 visible in worker logs) will not retroactively complete; only new submissions will succeed.
- **Operators**: documents the lesson that `init.sql` and the migration ledger can disagree, and provides a repeatable repair recipe.
- **Risk**: low. Both target migrations use `IF NOT EXISTS` on every DDL, so reapplying is idempotent. Only DML touched is `schema_migrations` itself, scoped to two filenames.
