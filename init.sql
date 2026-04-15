-- Hydra MVP Database Schema
-- Version: 1.3.0
-- 21 base tables + entity graph + sprint extensions + shadow mode (v0.10.0), views, append-only enforcement

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ============================================================
-- TENANT & AUTH
-- ============================================================

CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    tier VARCHAR(50) NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'professional', 'enterprise')),
    settings JSONB NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(255),
    password_hash TEXT,
    role VARCHAR(20) NOT NULL DEFAULT 'analyst' CHECK (role IN ('admin', 'analyst', 'viewer')),
    external_auth_id VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, email)
);

-- ============================================================
-- AGENT CONFIGURATION
-- ============================================================

CREATE TABLE agent_personas (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    system_prompt TEXT NOT NULL,
    model_name VARCHAR(100) NOT NULL DEFAULT 'fast',
    temperature NUMERIC(3,2) NOT NULL DEFAULT 0.7 CHECK (temperature >= 0 AND temperature <= 2),
    max_tokens INTEGER NOT NULL DEFAULT 2048,
    tools_enabled JSONB NOT NULL DEFAULT '[]',
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- SKILLS (Code Generation & Execution)
-- ============================================================

CREATE TABLE agent_skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),  -- NULL = community/global
    skill_name VARCHAR(100) NOT NULL,
    skill_slug VARCHAR(100) NOT NULL,
    version INTEGER DEFAULT 1,
    is_community BOOLEAN DEFAULT true,
    threat_types TEXT[] NOT NULL,
    mitre_tactics TEXT[] NOT NULL,
    mitre_techniques TEXT[] NOT NULL,
    severity_default VARCHAR(20) DEFAULT 'medium',
    applicable_log_sources TEXT[],
    investigation_methodology TEXT NOT NULL,
    detection_patterns TEXT NOT NULL,
    code_template TEXT,
    example_prompt TEXT NOT NULL,
    expected_output_schema JSONB,
    follow_up_chain JSONB,
    embedding vector(768),
    keywords TEXT[],
    parameters JSONB,
    times_used INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, skill_slug, version)
);

-- ============================================================
-- TASKS & EXECUTION
-- ============================================================

CREATE TABLE agent_tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),
    persona_id UUID REFERENCES agent_personas(id),
    task_type VARCHAR(100) NOT NULL,
    input JSONB NOT NULL DEFAULT '{}',
    output JSONB,
    status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'planning', 'approved', 'executing', 'completed', 'failed', 'cancelled', 'awaiting_approval', 'rejected')),
    error_message TEXT,
    workflow_id VARCHAR(255),
    workflow_run_id VARCHAR(255),
    skill_id UUID REFERENCES agent_skills(id),
    tokens_used_input INTEGER NOT NULL DEFAULT 0,
    tokens_used_output INTEGER NOT NULL DEFAULT 0,
    execution_ms INTEGER,
    sandbox_container_id VARCHAR(255),
    severity VARCHAR(20),
    worker_id VARCHAR(255),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE agent_task_steps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    step_type VARCHAR(50) NOT NULL CHECK (step_type IN ('plan', 'llm_call', 'skill_exec', 'tool_call', 'human_approval', 'sandbox_exec')),
    input JSONB,
    output JSONB,
    status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    error_message TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    execution_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- MEMORY (Episodic + Semantic via pgvector + Investigation Memory)
-- ============================================================

CREATE TABLE investigation_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    task_id UUID NOT NULL REFERENCES agent_tasks(id),
    skill_used_id UUID REFERENCES agent_skills(id),
    threat_type VARCHAR(100),
    memory_summary TEXT NOT NULL,
    key_findings JSONB NOT NULL,
    key_iocs JSONB,
    risk_score INTEGER,
    effective_patterns TEXT[],
    embedding vector(768),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_memory_episodic (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    persona_id UUID REFERENCES agent_personas(id),
    task_id UUID REFERENCES agent_tasks(id),
    memory_type VARCHAR(50) NOT NULL DEFAULT 'task_outcome' CHECK (memory_type IN ('task_outcome', 'error_lesson', 'user_preference', 'skill_feedback')),
    content TEXT NOT NULL,
    embedding vector(768),
    importance_score NUMERIC(3,2) NOT NULL DEFAULT 0.5 CHECK (importance_score >= 0 AND importance_score <= 1),
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- USAGE METERING
-- ============================================================

CREATE TABLE usage_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    task_id UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
    record_type VARCHAR(50) NOT NULL CHECK (record_type IN ('llm_call', 'embedding', 'skill_exec', 'storage')),
    model_name VARCHAR(100),
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
    execution_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- AUDIT LOG (Immutable, append-only)
-- ============================================================

CREATE TABLE agent_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    resource_id UUID,
    details JSONB NOT NULL DEFAULT '{}',
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- OBJECT STORAGE REFERENCES
-- ============================================================

CREATE TABLE object_refs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    bucket VARCHAR(100) NOT NULL,
    object_key VARCHAR(500) NOT NULL,
    content_type VARCHAR(255),
    size_bytes BIGINT,
    checksum_sha256 VARCHAR(64),
    uploaded_by UUID REFERENCES users(id),
    task_id UUID REFERENCES agent_tasks(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(bucket, object_key)
);

-- ============================================================
-- WORKING MEMORY (Redis-backed, but schema for overflow/persistence)
-- ============================================================

CREATE TABLE working_memory_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    snapshot_data JSONB NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- SIEM INTEGRATION
-- ============================================================

CREATE TABLE log_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name VARCHAR(255) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    connection_config JSONB NOT NULL DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    last_event_at TIMESTAMPTZ,
    event_count INTEGER DEFAULT 0,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE siem_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    log_source_id UUID NOT NULL REFERENCES log_sources(id),
    task_id UUID REFERENCES agent_tasks(id),
    alert_name VARCHAR(500) NOT NULL,
    severity VARCHAR(20),
    source_ip VARCHAR(45),
    dest_ip VARCHAR(45),
    rule_name VARCHAR(500),
    raw_event JSONB NOT NULL,
    normalized_event JSONB NOT NULL,
    status VARCHAR(20) DEFAULT 'new',
    auto_investigate BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- INVESTIGATION STEPS
-- ============================================================

CREATE TABLE investigation_steps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    step_type VARCHAR(50) NOT NULL DEFAULT 'analysis',
    summary_prompt TEXT,
    generated_code TEXT,
    output TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    tokens_used_input INTEGER DEFAULT 0,
    tokens_used_output INTEGER DEFAULT 0,
    execution_ms INTEGER DEFAULT 0,
    execution_mode VARCHAR(20) DEFAULT 'sandbox',
    parameters_used JSONB,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(task_id, step_number)
);

-- ============================================================
-- APPROVAL REQUESTS
-- ============================================================

CREATE TABLE approval_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    risk_level VARCHAR(20) NOT NULL DEFAULT 'medium',
    action_summary TEXT,
    generated_code TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ,
    decided_by UUID REFERENCES users(id),
    decision_comment TEXT
);

-- ============================================================
-- PLAYBOOKS
-- ============================================================

CREATE TABLE playbooks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    icon VARCHAR(50),
    task_type VARCHAR(100) NOT NULL,
    is_template BOOLEAN NOT NULL DEFAULT false,
    system_prompt_override TEXT,
    steps JSONB NOT NULL DEFAULT '[]',
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_agent_personas_tenant ON agent_personas(tenant_id);
CREATE INDEX idx_skills_tenant ON agent_skills(tenant_id);
CREATE INDEX idx_skills_threat ON agent_skills USING GIN(threat_types);
CREATE INDEX idx_skills_mitre ON agent_skills USING GIN(mitre_techniques);
CREATE INDEX idx_skills_keywords ON agent_skills USING GIN(keywords);
CREATE INDEX idx_skills_embedding ON agent_skills USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_memory_tenant ON investigation_memory(tenant_id);
CREATE INDEX idx_investigation_memory_embedding ON investigation_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_agent_tasks_tenant_status ON agent_tasks(tenant_id, status);
CREATE INDEX idx_agent_tasks_workflow ON agent_tasks(workflow_id);
CREATE INDEX idx_agent_task_steps_task ON agent_task_steps(task_id);
CREATE INDEX idx_agent_memory_tenant ON agent_memory_episodic(tenant_id);
CREATE INDEX idx_agent_memory_type ON agent_memory_episodic(tenant_id, memory_type);
CREATE INDEX idx_usage_records_tenant ON usage_records(tenant_id);
CREATE INDEX idx_usage_records_created ON usage_records(tenant_id, created_at);
CREATE INDEX idx_audit_log_tenant ON agent_audit_log(tenant_id);
CREATE INDEX idx_audit_log_created ON agent_audit_log(tenant_id, created_at);
CREATE INDEX idx_audit_log_action ON agent_audit_log(action);
CREATE INDEX idx_object_refs_tenant ON object_refs(tenant_id);
CREATE INDEX idx_log_sources_tenant ON log_sources(tenant_id);
CREATE INDEX idx_siem_alerts_tenant ON siem_alerts(tenant_id);
CREATE INDEX idx_siem_alerts_status ON siem_alerts(status);
CREATE INDEX idx_siem_alerts_source ON siem_alerts(log_source_id);
CREATE INDEX idx_siem_alerts_created ON siem_alerts(created_at DESC);
CREATE INDEX idx_investigation_steps_task ON investigation_steps(task_id);
CREATE INDEX idx_approval_requests_task ON approval_requests(task_id);
CREATE INDEX idx_approval_requests_status ON approval_requests(status);
CREATE INDEX idx_playbooks_tenant ON playbooks(tenant_id);

-- Vector similarity index for episodic memory
CREATE INDEX idx_memory_embedding ON agent_memory_episodic
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- APPEND-ONLY ENFORCEMENT (audit_log + usage_records)
-- ============================================================

CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: modifications are not permitted';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_immutable
    BEFORE UPDATE OR DELETE ON agent_audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();

CREATE TRIGGER usage_records_immutable
    BEFORE UPDATE OR DELETE ON usage_records
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();

-- ============================================================
-- VIEW: Task summary with cost
-- ============================================================

CREATE OR REPLACE VIEW v_task_summary AS
SELECT
    t.id AS task_id,
    t.tenant_id,
    tn.name AS tenant_name,
    t.task_type,
    t.status,
    t.tokens_used_input,
    t.tokens_used_output,
    t.execution_ms,
    COALESCE(SUM(u.cost_usd), 0) AS total_cost_usd,
    t.created_at,
    t.completed_at
FROM agent_tasks t
JOIN tenants tn ON tn.id = t.tenant_id
LEFT JOIN usage_records u ON u.task_id = t.id
GROUP BY t.id, tn.name;

-- ============================================================
-- SEED DATA (Development only)
-- ============================================================
-- database-seed-system change: the dev tenant + fixture users are now owned
-- by migrations/seed_dev_data.sql (mounted at /docker-entrypoint-initdb.d/
-- 02-seed-dev.sql). The old 'Hydra Dev' insert was removed to avoid two
-- parallel dev tenants. Canonical dev tenant: zovark-dev (UUID …0010).

-- ============================================================
-- ENTITY GRAPH (Sprint 1G)
-- ============================================================

-- Investigations (partitioned by month)
CREATE TABLE investigations (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID REFERENCES agent_tasks(id) ON DELETE CASCADE,
    alert_source VARCHAR(255),
    alert_type VARCHAR(100),
    skill_id UUID REFERENCES agent_skills(id),
    skill_version INTEGER,
    attack_techniques TEXT[],
    verdict VARCHAR(20) CHECK (verdict IN (
        'true_positive', 'false_positive', 'benign', 'suspicious', 'inconclusive'
    )),
    risk_score INTEGER CHECK (risk_score >= 0 AND risk_score <= 100),
    confidence NUMERIC(3,2) CHECK (confidence >= 0.0 AND confidence <= 1.0),
    timeline JSONB,
    summary TEXT,
    summary_embedding vector(768),
    model_id VARCHAR(100),
    model_version VARCHAR(100),
    prompt_version VARCHAR(50),
    analyst_feedback JSONB,
    source VARCHAR(20) NOT NULL DEFAULT 'production' CHECK (source IN (
        'production', 'bootstrap', 'synthetic'
    )),
    injection_detected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE investigations_2026_01 PARTITION OF investigations
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE investigations_2026_02 PARTITION OF investigations
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE investigations_2026_03 PARTITION OF investigations
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE investigations_2026_04 PARTITION OF investigations
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE investigations_2026_05 PARTITION OF investigations
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE investigations_2026_06 PARTITION OF investigations
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE investigations_2026_07 PARTITION OF investigations
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE investigations_2026_08 PARTITION OF investigations
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE investigations_2026_09 PARTITION OF investigations
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE investigations_2026_10 PARTITION OF investigations
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE investigations_2026_11 PARTITION OF investigations
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE investigations_2026_12 PARTITION OF investigations
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE investigations_default PARTITION OF investigations DEFAULT;

CREATE INDEX idx_investigations_attack_techniques
    ON investigations USING GIN (attack_techniques);
CREATE INDEX idx_investigations_verdict_source
    ON investigations (verdict, source);
CREATE INDEX idx_investigations_risk_score
    ON investigations (risk_score DESC);
CREATE INDEX idx_investigations_created_at
    ON investigations (created_at);
CREATE INDEX idx_investigations_tenant
    ON investigations (tenant_id);
CREATE INDEX idx_investigations_task
    ON investigations (task_id);
CREATE INDEX idx_investigations_summary_embedding
    ON investigations USING ivfflat (summary_embedding vector_cosine_ops) WITH (lists = 100);

-- Entities (normalized IOCs with cross-tenant dedup)
CREATE TABLE entities (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    entity_hash VARCHAR(64) NOT NULL,
    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN (
        'ip', 'domain', 'file_hash', 'url', 'user', 'device', 'process', 'email'
    )),
    value TEXT NOT NULL,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    threat_score INTEGER DEFAULT 0 CHECK (threat_score >= 0 AND threat_score <= 100),
    observation_count INTEGER DEFAULT 1,
    tenant_count INTEGER DEFAULT 1,
    metadata JSONB DEFAULT '{}',
    UNIQUE(entity_hash, tenant_id)
);

CREATE INDEX idx_entities_type_threat ON entities (entity_type, threat_score DESC);
CREATE INDEX idx_entities_last_seen ON entities (last_seen DESC);
CREATE INDEX idx_entities_tenant ON entities (tenant_id);
CREATE INDEX idx_entities_hash ON entities (entity_hash);

-- Entity edges (relationships between entities)
CREATE TABLE entity_edges (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    source_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    edge_type VARCHAR(30) NOT NULL CHECK (edge_type IN (
        'communicates_with', 'resolved_to', 'logged_into',
        'executed', 'downloaded', 'contains', 'parent_of',
        'accessed', 'sent_to', 'received_from', 'associated_with'
    )),
    investigation_id UUID,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    mitre_technique VARCHAR(20),
    confidence NUMERIC(3,2) DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX idx_entity_edges_source_type ON entity_edges (source_entity_id, edge_type);
CREATE INDEX idx_entity_edges_target ON entity_edges (target_entity_id);
CREATE INDEX idx_entity_edges_investigation ON entity_edges (investigation_id);
CREATE INDEX idx_entity_edges_tenant ON entity_edges (tenant_id);
CREATE INDEX idx_entity_edges_mitre ON entity_edges (mitre_technique) WHERE mitre_technique IS NOT NULL;

-- Entity observations (entity sightings per investigation)
CREATE TABLE entity_observations (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    investigation_id UUID,
    role VARCHAR(20) NOT NULL CHECK (role IN (
        'source', 'destination', 'attacker', 'victim',
        'indicator', 'artifact', 'infrastructure', 'target'
    )),
    context TEXT,
    mitre_technique VARCHAR(20),
    confidence_source VARCHAR(20) DEFAULT 'clean' CHECK (confidence_source IN ('clean', 'suspicious', 'injection_detected')),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entity_observations_entity ON entity_observations (entity_id);
CREATE INDEX idx_entity_observations_investigation ON entity_observations (investigation_id);
CREATE INDEX idx_entity_observations_role ON entity_observations (role);
CREATE INDEX idx_entity_observations_mitre ON entity_observations (mitre_technique) WHERE mitre_technique IS NOT NULL;
CREATE INDEX idx_entity_obs_confidence ON entity_observations(confidence_source);

-- ============================================================
-- AUDIT EVENTS (Structured, partitioned, append-only)
-- Sprint 1E-4, updated Sprint 1K
-- ============================================================

CREATE TABLE audit_events (
    id BIGSERIAL,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    event_type VARCHAR(50) NOT NULL CHECK (event_type IN (
        'investigation_started', 'investigation_completed', 'code_executed',
        'approval_requested', 'approval_granted', 'approval_denied', 'approval_timeout',
        'entity_extracted', 'detection_generated', 'user_login', 'user_registered',
        'injection_detected', 'cross_tenant_hit', 'threat_score_updated',
        'self_healing_scan', 'self_healing_diagnosis', 'self_healing_patch_applied',
        'self_healing_patch_failed', 'self_healing_rollback'
    )),
    actor_id UUID,
    actor_type VARCHAR(20) CHECK (actor_type IN ('user', 'worker', 'system')),
    resource_type VARCHAR(50),
    resource_id UUID,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE audit_events_2026_01 PARTITION OF audit_events
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE audit_events_2026_02 PARTITION OF audit_events
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE audit_events_2026_03 PARTITION OF audit_events
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE audit_events_2026_04 PARTITION OF audit_events
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE audit_events_2026_05 PARTITION OF audit_events
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE audit_events_2026_06 PARTITION OF audit_events
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE audit_events_2026_07 PARTITION OF audit_events
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE audit_events_2026_08 PARTITION OF audit_events
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE audit_events_2026_09 PARTITION OF audit_events
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE audit_events_2026_10 PARTITION OF audit_events
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE audit_events_2026_11 PARTITION OF audit_events
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE audit_events_2026_12 PARTITION OF audit_events
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;

CREATE INDEX idx_audit_events_tenant ON audit_events (tenant_id);
CREATE INDEX idx_audit_events_type ON audit_events (event_type);
CREATE INDEX idx_audit_events_created ON audit_events (created_at);
CREATE INDEX idx_audit_events_resource ON audit_events (resource_type, resource_id);

-- ============================================================
-- MITRE ATT&CK TECHNIQUES (Sprint 1F)
-- ============================================================

CREATE TABLE mitre_techniques (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    technique_id VARCHAR(20) NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    tactics TEXT[],
    platforms TEXT[],
    data_sources TEXT[],
    detection TEXT,
    url TEXT,
    embedding vector(768),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_mitre_technique_id ON mitre_techniques (technique_id);
CREATE INDEX idx_mitre_tactics ON mitre_techniques USING GIN (tactics);
CREATE INDEX idx_mitre_embedding ON mitre_techniques USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- ============================================================
-- BOOTSTRAP CORPUS (Sprint 1F)
-- ============================================================

CREATE TABLE bootstrap_corpus (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    source VARCHAR(20) NOT NULL CHECK (source IN ('mitre', 'cisa', 'synthetic')),
    source_id VARCHAR(50),
    title TEXT,
    description TEXT,
    generated_investigation TEXT,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'generating', 'completed', 'failed')),
    entity_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_bootstrap_source ON bootstrap_corpus (source, status);
CREATE INDEX idx_bootstrap_source_id ON bootstrap_corpus (source_id);

-- ============================================================
-- INVESTIGATION REPORTS (Sprint 1L)
-- ============================================================

CREATE TABLE investigation_reports (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    investigation_id UUID NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    report_format VARCHAR(10) NOT NULL CHECK (report_format IN ('markdown', 'pdf')),
    executive_summary TEXT,
    technical_timeline TEXT,
    remediation_steps TEXT,
    full_report TEXT,
    pdf_data BYTEA,
    generated_by VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_reports_investigation ON investigation_reports(investigation_id);
CREATE INDEX idx_reports_tenant ON investigation_reports(tenant_id);

-- ============================================================
-- CROSS-TENANT INTELLIGENCE (Sprint 1K)
-- ============================================================

-- Materialized view: entity threat scores aggregated across tenants
CREATE MATERIALIZED VIEW IF NOT EXISTS cross_tenant_intel AS
SELECT
    e.entity_hash,
    e.entity_type,
    COUNT(DISTINCT e.tenant_id) as tenant_count,
    COUNT(DISTINCT eo.investigation_id) as investigation_count,
    MAX(e.observation_count) as max_observations,
    MAX(e.threat_score) as max_threat_score,
    array_agg(DISTINCT e.tenant_id) as tenant_ids,
    MAX(e.last_seen) as last_seen_globally
FROM entities e
JOIN entity_observations eo ON eo.entity_id = e.id
JOIN investigations i ON i.id = eo.investigation_id
WHERE NOT COALESCE(i.injection_detected, false)
GROUP BY e.entity_hash, e.entity_type
HAVING COUNT(DISTINCT e.tenant_id) >= 2;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cross_tenant_hash ON cross_tenant_intel(entity_hash);
CREATE INDEX IF NOT EXISTS idx_cross_tenant_type ON cross_tenant_intel(entity_type);
CREATE INDEX IF NOT EXISTS idx_cross_tenant_tenants ON cross_tenant_intel(tenant_count DESC);

-- Privacy-safe view: strips tenant_ids, only shows aggregate counts
CREATE OR REPLACE VIEW cross_tenant_public AS
SELECT
    entity_hash,
    entity_type,
    tenant_count,
    investigation_count,
    max_observations,
    max_threat_score,
    last_seen_globally
FROM cross_tenant_intel;

-- ============================================================
-- LLM CALL LOG (Sprint 1I)
-- ============================================================

CREATE TABLE llm_call_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id),
    task_id UUID,
    activity_name VARCHAR(100) NOT NULL,
    model_tier VARCHAR(20) NOT NULL CHECK (model_tier IN ('fast', 'standard', 'reasoning')),
    model_id VARCHAR(200) NOT NULL,
    prompt_name VARCHAR(100),
    prompt_version VARCHAR(20),
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER GENERATED ALWAYS AS (input_tokens + output_tokens) STORED,
    estimated_cost_usd NUMERIC(10,6) DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'timeout', 'fallback')),
    error_message TEXT,
    temperature NUMERIC(3,2),
    max_tokens INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_llm_call_log_tenant ON llm_call_log(tenant_id);
CREATE INDEX idx_llm_call_log_activity ON llm_call_log(activity_name);
CREATE INDEX idx_llm_call_log_model ON llm_call_log(model_id);
CREATE INDEX idx_llm_call_log_prompt ON llm_call_log(prompt_name, prompt_version);
CREATE INDEX idx_llm_call_log_created ON llm_call_log(created_at DESC);
CREATE INDEX idx_llm_call_log_status ON llm_call_log(status) WHERE status != 'success';

-- Model performance materialized view
CREATE MATERIALIZED VIEW IF NOT EXISTS model_performance AS
SELECT
    model_id,
    activity_name,
    prompt_name,
    COUNT(*) as call_count,
    AVG(latency_ms) as avg_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_latency_ms,
    SUM(total_tokens) as total_tokens,
    SUM(estimated_cost_usd) as total_cost_usd,
    AVG(input_tokens) as avg_input_tokens,
    AVG(output_tokens) as avg_output_tokens,
    COUNT(*) FILTER (WHERE status = 'error') as error_count,
    COUNT(*) FILTER (WHERE status = 'timeout') as timeout_count,
    COUNT(*) FILTER (WHERE status = 'fallback') as fallback_count,
    MIN(created_at) as first_seen,
    MAX(created_at) as last_seen
FROM llm_call_log
GROUP BY model_id, activity_name, prompt_name;

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_perf_key
    ON model_performance(model_id, activity_name, prompt_name);

-- ============================================================
-- DETECTION ENGINE (Sprint 2A)
-- ============================================================

CREATE TABLE detection_candidates (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    technique_id VARCHAR(20) NOT NULL,
    pattern_signature VARCHAR(64) NOT NULL UNIQUE,
    pattern_description TEXT,
    entity_types TEXT[],
    edge_patterns TEXT[],
    investigation_count INTEGER,
    tenant_spread INTEGER,
    avg_risk_score FLOAT,
    status VARCHAR(20) DEFAULT 'candidate' CHECK (status IN (
        'candidate', 'generating', 'validating', 'approved', 'deployed', 'rejected', 'retired'
    )),
    sigma_rule TEXT,
    validation_result JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    deployed_at TIMESTAMPTZ
);

CREATE TABLE detection_rules (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    candidate_id UUID REFERENCES detection_candidates(id),
    technique_id VARCHAR(20) NOT NULL,
    rule_name VARCHAR(255) NOT NULL,
    rule_version INTEGER NOT NULL DEFAULT 1,
    sigma_yaml TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'testing', 'retired')),
    tp_rate FLOAT,
    fp_rate FLOAT,
    investigations_matched INTEGER,
    tenant_spread INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    UNIQUE(technique_id, rule_version)
);

CREATE INDEX idx_candidates_technique ON detection_candidates(technique_id);
CREATE INDEX idx_candidates_status ON detection_candidates(status);
CREATE INDEX idx_rules_technique ON detection_rules(technique_id);
CREATE INDEX idx_rules_status ON detection_rules(status);

-- ============================================================
-- SOAR RESPONSE PLAYBOOKS (Sprint 2B)
-- ============================================================

CREATE TABLE IF NOT EXISTS response_playbooks (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    trigger_conditions JSONB NOT NULL,
    actions JSONB NOT NULL,
    requires_approval BOOLEAN DEFAULT true,
    enabled BOOLEAN DEFAULT true,
    tenant_id UUID REFERENCES tenants(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS response_executions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    playbook_id UUID REFERENCES response_playbooks(id),
    investigation_id UUID,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    trigger_data JSONB,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN (
        'pending', 'awaiting_approval', 'executing', 'completed',
        'failed', 'rolled_back', 'cancelled'
    )),
    actions_executed JSONB DEFAULT '[]',
    approved_by UUID,
    approved_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS response_integrations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    integration_type VARCHAR(50) NOT NULL,
    name VARCHAR(255),
    webhook_url TEXT NOT NULL,
    auth_type VARCHAR(20) CHECK (auth_type IN ('none', 'bearer', 'api_key', 'basic')),
    auth_credentials TEXT,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_playbooks_tenant ON response_playbooks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_playbooks_enabled ON response_playbooks(enabled);
CREATE INDEX IF NOT EXISTS idx_response_exec_tenant ON response_executions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_response_exec_status ON response_executions(status);
CREATE INDEX IF NOT EXISTS idx_response_exec_investigation ON response_executions(investigation_id);
CREATE INDEX IF NOT EXISTS idx_integrations_tenant ON response_integrations(tenant_id);

-- ============================================================
-- WEBHOOK ENDPOINTS & DELIVERIES (Sprint 3C)
-- ============================================================

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_concurrent INT DEFAULT 50;

CREATE TABLE IF NOT EXISTS webhook_endpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,
    secret VARCHAR(255),
    event_types TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_tenant ON webhook_endpoints(tenant_id);
CREATE INDEX IF NOT EXISTS idx_webhook_endpoints_active ON webhook_endpoints(tenant_id, is_active) WHERE is_active = true;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint_id UUID NOT NULL REFERENCES webhook_endpoints(id),
    tenant_id UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed', 'retrying')),
    http_status INT,
    response_body TEXT,
    attempts INT DEFAULT 0,
    max_attempts INT DEFAULT 3,
    last_attempt_at TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint ON webhook_deliveries(endpoint_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries(status, next_retry_at) WHERE status IN ('pending', 'retrying');
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_tenant ON webhook_deliveries(tenant_id, created_at DESC);

-- ============================================================
-- FINE-TUNING PIPELINE (Sprint 3D)
-- ============================================================

CREATE TABLE IF NOT EXISTS finetuning_jobs (
    id VARCHAR(100) PRIMARY KEY,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'exporting', 'scoring', 'evaluating', 'completed', 'failed', 'skipped')),
    config JSONB DEFAULT '{}',
    training_examples INT DEFAULT 0,
    quality_stats JSONB DEFAULT '{}',
    evaluation_results JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_finetuning_jobs_status ON finetuning_jobs(status);
CREATE INDEX IF NOT EXISTS idx_finetuning_jobs_created ON finetuning_jobs(created_at DESC);

-- ============================================================
-- MODEL REGISTRY + A/B TESTING (Sprint 3E)
-- ============================================================

CREATE TABLE IF NOT EXISTS model_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    provider VARCHAR(100) NOT NULL,
    model_id VARCHAR(255) NOT NULL,
    version VARCHAR(50) DEFAULT '1.0',
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'testing', 'promoted', 'deprecated')),
    is_default BOOLEAN DEFAULT false,
    config JSONB DEFAULT '{}',
    routing_rules JSONB DEFAULT '{}',
    eval_score FLOAT,
    eval_results JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_default ON model_registry(is_default) WHERE is_default = true;
CREATE INDEX IF NOT EXISTS idx_model_registry_status ON model_registry(status);

CREATE TABLE IF NOT EXISTS model_ab_tests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    model_a_id UUID NOT NULL REFERENCES model_registry(id),
    model_b_id UUID NOT NULL REFERENCES model_registry(id),
    traffic_split FLOAT DEFAULT 0.5,
    status VARCHAR(20) DEFAULT 'running' CHECK (status IN ('running', 'completed', 'cancelled')),
    results_a JSONB DEFAULT '{}',
    results_b JSONB DEFAULT '{}',
    winner_id UUID REFERENCES model_registry(id),
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ab_tests_status ON model_ab_tests(status);

-- ============================================================
-- SECURITY & COMPLIANCE (Sprint 3F)
-- ============================================================

CREATE TABLE IF NOT EXISTS data_retention_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name VARCHAR(100) NOT NULL UNIQUE,
    retention_days INT NOT NULL DEFAULT 90,
    delete_strategy VARCHAR(20) DEFAULT 'soft' CHECK (delete_strategy IN ('soft', 'hard', 'archive')),
    is_active BOOLEAN DEFAULT true,
    last_cleanup_at TIMESTAMPTZ,
    rows_cleaned INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO data_retention_policies (table_name, retention_days, delete_strategy) VALUES
    ('agent_audit_log', 365, 'archive'),
    ('webhook_deliveries', 30, 'hard'),
    ('usage_records', 90, 'hard'),
    ('investigation_steps', 180, 'archive'),
    ('siem_alerts', 90, 'soft')
ON CONFLICT (table_name) DO NOTHING;

ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

-- ============================================================
-- SELF-HEALING SRE AGENT (Sprint 4A)
-- ============================================================

CREATE TABLE IF NOT EXISTS self_healing_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    failure_id TEXT,
    workflow_id TEXT,
    activity_name VARCHAR(100),
    error_category VARCHAR(30) CHECK (error_category IN (
        'dependency_missing', 'logic_bug', 'llm_malformed', 'resource_exhaustion', 'unknown'
    )),
    diagnosis JSONB,
    patch_type VARCHAR(30),
    patch_content TEXT,
    test_result JSONB,
    applied BOOLEAN DEFAULT false,
    rolled_back BOOLEAN DEFAULT false,
    file_path TEXT,
    backup_path TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_self_healing_created ON self_healing_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_self_healing_category ON self_healing_events(error_category);

-- ============================================================
-- SPRINT 5: ALERT DEDUPLICATION + VALIDATION
-- ============================================================

-- Update audit_events CHECK to add Sprint 5 event types
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_event_type_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (event_type IN (
    'investigation_started', 'investigation_completed', 'code_executed',
    'approval_requested', 'approval_granted', 'approval_denied', 'approval_timeout',
    'entity_extracted', 'detection_generated', 'user_login', 'user_registered',
    'injection_detected', 'cross_tenant_hit', 'threat_score_updated',
    'self_healing_scan', 'self_healing_diagnosis', 'self_healing_patch_applied',
    'self_healing_patch_failed', 'self_healing_rollback',
    'dry_run_validation_failed', 'memory_enrichment_applied',
    'playbook_auto_triggered', 'retrain_check', 'sla_breach'
));

-- Alert deduplication via composite fingerprinting
CREATE TABLE IF NOT EXISTS alert_fingerprints (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    fingerprint     VARCHAR(64) NOT NULL,  -- SHA-256 hex
    alert_type      VARCHAR(100) NOT NULL,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_count     INTEGER NOT NULL DEFAULT 1,
    investigation_id UUID,  -- references investigations(id) logically, no FK due to partitioned table
    dedup_window_seconds INTEGER NOT NULL DEFAULT 900,  -- 15 min default
    raw_sample      JSONB,  -- first alert payload for reference
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_fp_tenant_hash ON alert_fingerprints(tenant_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_alert_fp_last_seen ON alert_fingerprints(last_seen);

-- ============================================================
-- INVESTIGATION FEEDBACK (Sprint 7C — Migration 017)
-- ============================================================

CREATE TABLE IF NOT EXISTS investigation_feedback (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    investigation_id UUID NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    analyst_id UUID REFERENCES users(id),
    verdict_correct BOOLEAN,
    corrected_verdict VARCHAR(30),
    false_positive BOOLEAN DEFAULT false,
    missed_threat BOOLEAN DEFAULT false,
    notes TEXT,
    analyst_confidence FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_investigation ON investigation_feedback(investigation_id);
CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON investigation_feedback(tenant_id);
CREATE INDEX IF NOT EXISTS idx_feedback_verdict ON investigation_feedback(verdict_correct);

-- ============================================================
-- COST TRACKING (Sprint 7D — Migration 018)
-- ============================================================

ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS cost_usd DECIMAL(10,6);

CREATE OR REPLACE VIEW investigation_costs AS
SELECT
    task_id,
    COUNT(*) as llm_calls,
    SUM(input_tokens) as total_input_tokens,
    SUM(output_tokens) as total_output_tokens,
    SUM(cost_usd) as total_cost_usd
FROM llm_call_log
WHERE task_id IS NOT NULL
GROUP BY task_id;

-- ============================================================
-- INVESTIGATION CACHE (Sprint 7E — Migration 019)
-- ============================================================

CREATE TABLE IF NOT EXISTS investigation_cache (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    cache_key VARCHAR(64) NOT NULL,
    investigation_id UUID NOT NULL,
    task_id UUID,
    verdict VARCHAR(30),
    risk_score FLOAT,
    confidence FLOAT,
    entity_count INTEGER,
    summary TEXT,
    ttl_hours INTEGER DEFAULT 24,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_key ON investigation_cache(cache_key);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON investigation_cache(expires_at);

-- ============================================================
-- FAILURE CONTEXT (Sprint 8 — Migration 020)
-- ============================================================

-- blocked_credentials status already included in agent_tasks CHECK above
-- Failure mode audit event types already included in audit_events CHECK above
-- Partial indexes for failure mode queries
CREATE INDEX IF NOT EXISTS idx_audit_events_failure_modes
    ON audit_events (event_type, created_at DESC)
    WHERE event_type IN ('model_timeout', 'telemetry_access_denied', 'schema_validation_error', 'postgres_lock');

CREATE INDEX IF NOT EXISTS idx_agent_tasks_blocked
    ON agent_tasks (tenant_id, status)
    WHERE status = 'blocked_credentials';

-- ============================================================
-- HNSW VECTOR INDEXES (Sprint 9A — Migration 021)
-- ============================================================

-- Add embedding columns to entities and entity_edges
ALTER TABLE entities ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE entity_edges ADD COLUMN IF NOT EXISTS embedding vector(768);

-- HNSW indexes (m=16, ef_construction=200 for high recall)
CREATE INDEX IF NOT EXISTS idx_entities_embedding_hnsw
    ON entities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_entity_edges_embedding_hnsw
    ON entity_edges USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_skills_embedding_hnsw
    ON agent_skills USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_investigation_memory_embedding_hnsw
    ON investigation_memory USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_memory_episodic_embedding_hnsw
    ON agent_memory_episodic USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_investigations_summary_embedding_hnsw
    ON investigations USING hnsw (summary_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_mitre_embedding_hnsw
    ON mitre_techniques USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- ============================================================
-- API KEYS (Sprint 9A — Migration 022)
-- ============================================================

CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT '{read}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(tenant_id, is_active) WHERE is_active = true;

-- ============================================================
-- TOTP 2FA (Sprint 9A — Migration 023)
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(64);
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT false;

-- ============================================================
-- SCHEDULED WORKFLOWS (Sprint 9B — Migration 024)
-- ============================================================

CREATE TABLE IF NOT EXISTS scheduled_workflows (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    workflow_type VARCHAR(100) NOT NULL,
    cron_expression VARCHAR(100) NOT NULL,
    params JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_workflows_active ON scheduled_workflows(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_scheduled_workflows_type ON scheduled_workflows(workflow_type);

INSERT INTO scheduled_workflows (workflow_type, cron_expression, params, is_active) VALUES
    ('DetectionGenerationWorkflow', '0 2 * * *', '{"min_investigations": 2}', true),
    ('SelfHealingWorkflow', '*/30 * * * *', '{"lookback_minutes": 30, "dry_run": true}', true),
    ('CrossTenantRefreshWorkflow', '0 * * * *', '{}', true)
ON CONFLICT DO NOTHING;

-- ============================================================
-- INCIDENTS (Sprint 9B — Migration 025)
-- ============================================================

CREATE TABLE IF NOT EXISTS incidents (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    title VARCHAR(500) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'medium' CHECK (severity IN ('critical', 'high', 'medium', 'low', 'informational')),
    alert_ids UUID[] DEFAULT '{}',
    alert_count INTEGER DEFAULT 0,
    correlation_rule VARCHAR(100),
    mitre_techniques TEXT[],
    source_ips TEXT[],
    target_users TEXT[],
    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'resolved', 'closed', 'false_positive')),
    investigation_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_incidents_tenant ON incidents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at DESC);

-- ============================================================
-- SLA EVENTS (Sprint 9B — Migration 026)
-- ============================================================

CREATE TABLE IF NOT EXISTS sla_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    task_id UUID NOT NULL,
    severity VARCHAR(20) NOT NULL,
    sla_threshold_minutes INTEGER NOT NULL,
    actual_duration_minutes FLOAT NOT NULL,
    breached BOOLEAN DEFAULT false,
    breach_ratio FLOAT,
    webhook_sent BOOLEAN DEFAULT false,
    webhook_status INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sla_events_tenant ON sla_events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sla_events_breached ON sla_events(breached) WHERE breached = true;
CREATE INDEX IF NOT EXISTS idx_sla_events_created ON sla_events(created_at DESC);

-- ============================================================
-- SHADOW MODE (Sprint v0.10.0 — Migration 027)
-- ============================================================

CREATE TABLE IF NOT EXISTS shadow_recommendations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    task_id UUID REFERENCES agent_tasks(id),
    investigation_id UUID,
    alert_type VARCHAR(100),
    alert_source VARCHAR(100),
    recommended_action VARCHAR(50) NOT NULL,
    recommended_severity VARCHAR(20),
    recommendation_reasoning TEXT,
    confidence FLOAT CHECK (confidence >= 0 AND confidence <= 1),
    recommended_playbook_id UUID REFERENCES response_playbooks(id),
    human_action VARCHAR(50),
    human_severity VARCHAR(20),
    human_reasoning TEXT,
    decided_by UUID REFERENCES users(id),
    decided_at TIMESTAMPTZ,
    action_match BOOLEAN,
    severity_match BOOLEAN,
    match_category VARCHAR(20),
    mismatch_reason TEXT,
    model_id VARCHAR(100),
    prompt_version VARCHAR(20),
    processing_time_ms INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shadow_rec_tenant ON shadow_recommendations(tenant_id);
CREATE INDEX IF NOT EXISTS idx_shadow_rec_status ON shadow_recommendations(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_shadow_rec_match ON shadow_recommendations(tenant_id, action_match) WHERE human_action IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_shadow_rec_created ON shadow_recommendations(created_at);
CREATE INDEX IF NOT EXISTS idx_shadow_rec_task ON shadow_recommendations(task_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS shadow_conformance_stats AS
SELECT
    tenant_id,
    alert_type,
    COUNT(*) AS total_recommendations,
    COUNT(*) FILTER (WHERE human_action IS NOT NULL) AS total_decided,
    COUNT(*) FILTER (WHERE action_match = true) AS exact_matches,
    COUNT(*) FILTER (WHERE severity_match = true) AS severity_matches,
    COUNT(*) FILTER (WHERE match_category = 'override') AS overrides,
    COUNT(*) FILTER (WHERE match_category = 'rejection') AS rejections,
    ROUND(AVG(CASE WHEN action_match THEN 1.0 ELSE 0.0 END)::numeric, 4) AS action_match_rate,
    ROUND(AVG(confidence)::numeric, 4) AS avg_confidence,
    ROUND(AVG(processing_time_ms)::numeric, 0) AS avg_processing_ms,
    MAX(created_at) AS last_recommendation_at
FROM shadow_recommendations
WHERE human_action IS NOT NULL
GROUP BY tenant_id, alert_type
WITH NO DATA;

CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_conformance_tenant_alert ON shadow_conformance_stats(tenant_id, alert_type);

-- ============================================================
-- TOKEN QUOTAS (Sprint v0.10.0 — Migration 028)
-- ============================================================

CREATE TABLE IF NOT EXISTS token_quotas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) UNIQUE,
    monthly_token_limit BIGINT NOT NULL DEFAULT 10000000,
    monthly_cost_limit_usd NUMERIC(10,2) DEFAULT 500.00,
    current_period_start DATE NOT NULL DEFAULT date_trunc('month', CURRENT_DATE)::date,
    tokens_used BIGINT NOT NULL DEFAULT 0,
    cost_used_usd NUMERIC(10,2) NOT NULL DEFAULT 0.00,
    warn_threshold_pct INTEGER NOT NULL DEFAULT 80,
    hard_limit_pct INTEGER NOT NULL DEFAULT 100,
    circuit_breaker_open BOOLEAN NOT NULL DEFAULT false,
    circuit_breaker_reason TEXT,
    circuit_breaker_opened_at TIMESTAMPTZ,
    circuit_breaker_opened_by UUID REFERENCES users(id),
    last_reset_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_quotas_tenant ON token_quotas(tenant_id);
CREATE INDEX IF NOT EXISTS idx_token_quotas_circuit ON token_quotas(circuit_breaker_open) WHERE circuit_breaker_open = true;

CREATE TABLE IF NOT EXISTS token_usage_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    task_id UUID REFERENCES agent_tasks(id),
    model_id VARCHAR(100),
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    tokens_total INTEGER GENERATED ALWAYS AS (tokens_input + tokens_output) STORED,
    cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
    quota_pct_after NUMERIC(5,2),
    throttled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_period ON token_usage_events(tenant_id, created_at);

-- ============================================================
-- KILL SWITCH (Sprint v0.10.0 — Migration 029)
-- ============================================================

CREATE TABLE IF NOT EXISTS automation_controls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    scope VARCHAR(30) NOT NULL DEFAULT 'tenant',
    scope_target VARCHAR(200),
    enabled BOOLEAN NOT NULL DEFAULT true,
    mode VARCHAR(20) NOT NULL DEFAULT 'shadow',
    killed BOOLEAN NOT NULL DEFAULT false,
    killed_by UUID REFERENCES users(id),
    killed_at TIMESTAMPTZ,
    kill_reason TEXT,
    auto_resume_at TIMESTAMPTZ,
    last_changed_by UUID REFERENCES users(id),
    last_changed_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, scope, scope_target)
);

CREATE INDEX IF NOT EXISTS idx_automation_controls_tenant ON automation_controls(tenant_id);
CREATE INDEX IF NOT EXISTS idx_automation_controls_killed ON automation_controls(killed) WHERE killed = true;

CREATE TABLE IF NOT EXISTS kill_switch_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    control_id UUID NOT NULL REFERENCES automation_controls(id),
    action VARCHAR(20) NOT NULL,
    old_state JSONB,
    new_state JSONB,
    actor_id UUID REFERENCES users(id),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kill_audit_tenant ON kill_switch_audit(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kill_audit_control ON kill_switch_audit(control_id);

-- ============================================================
-- PII DETECTION (Sprint v0.10.0 — Migration 030)
-- ============================================================

CREATE TABLE IF NOT EXISTS pii_detections (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    task_id UUID REFERENCES agent_tasks(id),
    field_path VARCHAR(200),
    pii_type VARCHAR(50) NOT NULL,
    detection_method VARCHAR(30) NOT NULL DEFAULT 'regex',
    original_hash VARCHAR(64),
    masked_value VARCHAR(200),
    entity_map_id VARCHAR(100),
    direction VARCHAR(10) NOT NULL,
    blocked BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pii_tenant ON pii_detections(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pii_type ON pii_detections(pii_type);
CREATE INDEX IF NOT EXISTS idx_pii_blocked ON pii_detections(blocked) WHERE blocked = true;

CREATE TABLE IF NOT EXISTS pii_masking_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id),
    pii_type VARCHAR(50) NOT NULL,
    action VARCHAR(20) NOT NULL DEFAULT 'mask',
    pattern TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pii_rules_tenant ON pii_masking_rules(tenant_id);

-- ============================================================
-- NATS STREAM TRACKING (Sprint v0.10.0 — Migration 031)
-- ============================================================

CREATE TABLE IF NOT EXISTS alert_stream_offsets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    stream_name VARCHAR(100) NOT NULL,
    consumer_name VARCHAR(100) NOT NULL,
    last_sequence BIGINT NOT NULL DEFAULT 0,
    last_processed_at TIMESTAMPTZ,
    lag INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, stream_name, consumer_name)
);

CREATE INDEX IF NOT EXISTS idx_stream_offsets_tenant ON alert_stream_offsets(tenant_id);

CREATE TABLE IF NOT EXISTS alert_surge_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    alerts_per_second NUMERIC(10,2),
    queue_depth INTEGER,
    processing_lag_ms INTEGER,
    throttle_applied BOOLEAN NOT NULL DEFAULT false,
    backpressure_applied BOOLEAN NOT NULL DEFAULT false,
    alerts_dropped INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_surge_tenant ON alert_surge_events(tenant_id, created_at);

-- ============================================================
-- STAMPEDE PROTECTION (Sprint v0.10.0 — Migration 032)
-- ============================================================

CREATE TABLE IF NOT EXISTS coalescing_locks (
    cache_key VARCHAR(500) PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    locked_by VARCHAR(100) NOT NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    waiter_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_coalescing_expires ON coalescing_locks(expires_at);

-- Shadow mode tenant settings (Migration 032)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS approval_mode VARCHAR(20) NOT NULL DEFAULT 'shadow';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS shadow_mode_start DATE;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS shadow_mode_day INTEGER GENERATED ALWAYS AS (
    CASE WHEN shadow_mode_start IS NOT NULL
    THEN EXTRACT(DAY FROM (CURRENT_DATE - shadow_mode_start))::integer
    ELSE NULL END
) STORED;
