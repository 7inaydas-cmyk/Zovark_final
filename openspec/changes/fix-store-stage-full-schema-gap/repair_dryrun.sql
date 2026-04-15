-- Dry-run version of repair.sql — identical body, ROLLBACK instead of COMMIT.
-- See design.md §Decisions §4 and tasks.md §3.2.

BEGIN;

ALTER TABLE investigations ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT 'unknown';
ALTER TABLE agent_tasks    ADD COLUMN IF NOT EXISTS model_name TEXT DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS idx_investigations_model ON investigations(model_name);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_model    ON agent_tasks(model_name);

UPDATE schema_migrations
    SET source = 'manual_backfill',
        applied_at = now(),
        applied_by = CURRENT_USER
    WHERE filename = '047_add_model_name.sql';

ROLLBACK;
