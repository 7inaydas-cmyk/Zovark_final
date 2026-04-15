-- Manual ledger drift repair for migrations 040 and 046.
-- See openspec/changes/fix-store-stage-schema-blocker/design.md §Migration Plan.
-- DDL bodies copied verbatim from migrations/040_human_review_flags.sql and migrations/046_llm_audit_log.sql.
-- All DDL is idempotent (IF NOT EXISTS).

BEGIN;

-- ===== migration 040: human review flags =====
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS needs_human_review BOOLEAN DEFAULT FALSE;
ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS review_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_tasks_human_review
    ON agent_tasks(needs_human_review)
    WHERE needs_human_review = TRUE;

UPDATE schema_migrations
    SET source = 'manual_backfill',
        applied_at = now(),
        applied_by = CURRENT_USER
    WHERE filename = '040_human_review_flags.sql';

-- ===== migration 046: llm_audit_log =====
CREATE TABLE IF NOT EXISTS llm_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID,
    tenant_id UUID,
    stage TEXT NOT NULL,
    task_type TEXT,
    model_name TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    prompt_hash TEXT,
    status TEXT NOT NULL DEFAULT 'success',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_audit_task    ON llm_audit_log(task_id);
CREATE INDEX IF NOT EXISTS idx_llm_audit_tenant  ON llm_audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_audit_created ON llm_audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_audit_model   ON llm_audit_log(model_name);

UPDATE schema_migrations
    SET source = 'manual_backfill',
        applied_at = now(),
        applied_by = CURRENT_USER
    WHERE filename = '046_llm_audit_log.sql';

COMMIT;
