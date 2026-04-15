## Why

The previous change (`fix-store-stage-schema-blocker`) successfully repaired migrations 040 and 046, but its §5 verification gate failed on a *different* missing column: `agent_tasks.model_name`. Worker logs confirm the same lying-ledger pattern: `schema_migrations` claims `047_add_model_name.sql` is `init_sql`-applied (with `applied_by='init_sql_freeze_2026-04-13'`), but `init.sql` never contained those `ALTER TABLE` statements. Stage 5 Store still cannot complete. This change resolves the **specific** gap that blocks `store.py` rather than blanket-applying every suspect migration.

## What Changes

- Apply `migrations/047_add_model_name.sql` to add:
  - `agent_tasks.model_name TEXT DEFAULT 'unknown'` *(the hard blocker — uncaught in `_update_task_status`)*
  - `investigations.model_name TEXT DEFAULT 'unknown'` *(currently silently swallowed by `_create_investigation`'s try/except; fixing it restores the audit field)*
  - `idx_agent_tasks_model` and `idx_investigations_model` indexes
- Repair the `schema_migrations` ledger row for `047_add_model_name.sql`: `source='manual_backfill'`, `applied_at=now()`, `applied_by=CURRENT_USER`.
- Verify the repair end-to-end with a real `brute_force` task submission and confirm `status='completed'` plus a clean worker log.
- **Out of scope — investigation_memory column mismatch (6 columns)**: Phase A revealed that `init.sql:136` creates `investigation_memory` with shape `(id uuid, tenant_id, task_id, skill_used_id, threat_type, memory_summary, key_findings, key_iocs, risk_score, effective_patterns, embedding vector(768), created_at)`, while `migrations/045_investigation_memory.sql` defines a parallel-universe table `(id serial, task_type, alert_signature, code_template, iocs_found, findings_found, risk_score, success, error_type)` that has never been applied because the table name was already taken by init.sql. `store.py:_save_pattern` is wrapped in `try/except` and prints `Pattern save failed (non-fatal)` on every call — Stage 5 completes regardless. Per spec constraint "Do not modify store.py", and because no single migration can resolve this fork (it would require either dropping init.sql's table or rewriting `_save_pattern`), this stays a documented non-fatal warning. A separate change must reconcile the two schemas as a code-vs-DB decision.
- **Out of scope — the other 11 lying ledger rows** from the 13-row suspect list (033, 034, 038, 041_system_configs, 042, 043, 044, 048, 049, 051, 052, 053). `store.py` does not reference any objects from these migrations, so they cannot block Stage 5. They remain dormant drift to be addressed in a future audit.
- **Out of scope — `token_quotas.monthly_tokens_used`**: still a code-vs-schema bug, not a missing migration; tracked from the previous change.

## Capabilities

### Modified Capabilities
- `schema-migration-integrity`: extends the requirement set established by `fix-store-stage-schema-blocker` with a new requirement that gap analysis MUST be driven by actual SQL statement extraction from the consuming code, not by guessing which migrations are missing.

## Impact

- **Code**: none. No application code is touched.
- **DB objects added**: 2 columns (`agent_tasks.model_name`, `investigations.model_name`) and 2 indexes (`idx_agent_tasks_model`, `idx_investigations_model`).
- **DB ledger rows touched**: exactly 1 — `047_add_model_name.sql` rewritten to `manual_backfill`. The two repairs from the previous change (040, 046) are not touched.
- **Pipeline**: unblocks Stage 5 Store for new investigations. Combined with the previous change's repair, every column and table that `store.py` references for non-skipped (non-try/except) writes now exists in the live schema.
- **Risk**: low. `047_add_model_name.sql` uses `IF NOT EXISTS` on every DDL statement and is idempotent. The single-row ledger UPDATE is reversible. Total DDL: 2 `ALTER TABLE`, 2 `CREATE INDEX`, plus 1 `UPDATE schema_migrations`.
- **Operators**: confirms the gap-analysis-from-code methodology established here scales — a single grep of `store.py` SQL strings produced an exhaustive, deterministic gap list in under a minute.
