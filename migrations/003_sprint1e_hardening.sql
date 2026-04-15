-- Sprint 1E: Production Hardening Migration
-- Apply with: docker compose exec -T postgres psql -U hydra -d hydra < migrations/003_sprint1e_hardening.sql

-- ============================================================
-- 1E-3: SCRAM-SHA-256 Auth Migration
-- Re-hash password with SCRAM (requires postgresql.conf change first)
-- ============================================================

-- NOTE: ALTER SYSTEM cannot run inside a transaction block (migration runner uses implicit tx).
-- PG16 defaults to scram-sha-256 already. Skipping. Also, user is 'zovark' not 'hydra'.
-- ALTER SYSTEM SET password_encryption = 'scram-sha-256';
-- SELECT pg_reload_conf();
-- ALTER USER hydra PASSWORD 'hydra_dev_2026';

-- ============================================================
-- 1E-4: Structured Audit Events Table (partitioned)
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN (
        'investigation_started', 'investigation_completed', 'code_executed',
        'approval_requested', 'approval_granted', 'approval_denied', 'approval_timeout',
        'entity_extracted', 'detection_generated', 'user_login', 'user_registered'
    )),
    actor_id UUID,
    actor_type VARCHAR(20) CHECK (actor_type IN ('user', 'worker', 'system')),
    resource_type VARCHAR(50),
    resource_id UUID,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Monthly partitions for 2026
DO $$
DECLARE
    m INT;
    start_date TEXT;
    end_date TEXT;
    part_name TEXT;
BEGIN
    FOR m IN 1..12 LOOP
        start_date := '2026-' || LPAD(m::TEXT, 2, '0') || '-01';
        IF m < 12 THEN
            end_date := '2026-' || LPAD((m+1)::TEXT, 2, '0') || '-01';
        ELSE
            end_date := '2027-01-01';
        END IF;
        part_name := 'audit_events_2026_' || LPAD(m::TEXT, 2, '0');

        IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part_name) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF audit_events FOR VALUES FROM (%L) TO (%L)',
                part_name, start_date, end_date
            );
        END IF;
    END LOOP;
END $$;

-- Default partition for out-of-range dates
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'audit_events_default') THEN
        CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant ON audit_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events (event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_resource ON audit_events (resource_type, resource_id);
