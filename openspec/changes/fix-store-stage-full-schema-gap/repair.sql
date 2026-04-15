-- Manual ledger drift repair for migration 047_add_model_name.sql.
-- See openspec/changes/fix-store-stage-full-schema-gap/design.md §Migration Plan.
-- DDL body copied verbatim from migrations/047_add_model_name.sql.
-- All DDL is idempotent (IF NOT EXISTS).

BEGIN;

-- ===== migration 047: model_name on agent_tasks + investigations =====
ALTER TABLE investigations ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT 'unknown';
ALTER TABLE agent_tasks    ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS idx_investigations_model ON investigations(model_name);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_model    ON agent_tasks(model_name);

UPDATE schema_migrations
    SET source = 'manual_backfill',
        applied_at = now(),
        applied_by = CURRENT_USER
    WHERE filename = '047_add_model_name.sql';

COMMIT;
