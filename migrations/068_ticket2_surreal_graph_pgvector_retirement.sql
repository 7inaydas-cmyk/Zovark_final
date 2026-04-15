-- Migration 068: Ticket 2 — retire PostgreSQL entity graph + pgvector (SurrealDB canonical)
-- Idempotent. Preserves OLTP tables and agent_tasks.dedup_hash index from 065.

-- Audit 3.21: drop materialized views + views that depend on the entity graph
-- tables BEFORE the CASCADE drops them implicitly. Without this, the cascade
-- silently destroys `cross_tenant_intel` / `cross_tenant_public` and any code
-- still calling `REFRESH MATERIALIZED VIEW cross_tenant_intel` explodes on
-- the next tick. Re-creation (if needed) happens in a follow-up migration.
DROP MATERIALIZED VIEW IF EXISTS cross_tenant_intel CASCADE;
DROP VIEW IF EXISTS cross_tenant_public CASCADE;

-- HNSW / IVFFlat indexes on vector columns (drop before ALTER COLUMN)
DROP INDEX IF EXISTS idx_entities_embedding_hnsw;
DROP INDEX IF EXISTS idx_entity_edges_embedding_hnsw;
DROP INDEX IF EXISTS idx_skills_embedding_hnsw;
DROP INDEX IF EXISTS idx_investigation_memory_embedding_hnsw;
DROP INDEX IF EXISTS idx_memory_episodic_embedding_hnsw;
DROP INDEX IF EXISTS idx_investigations_summary_embedding_hnsw;
DROP INDEX IF EXISTS idx_investigations_summary_embedding;
DROP INDEX IF EXISTS idx_mitre_embedding_hnsw;
DROP INDEX IF EXISTS idx_mitre_embedding;
DROP INDEX IF EXISTS idx_fingerprints_embedding;
DROP INDEX IF EXISTS idx_memory_embedding;
DROP INDEX IF EXISTS idx_skills_embedding;

-- Entity graph tables (edges depend on entities)
DROP TABLE IF EXISTS entity_observations CASCADE;
DROP TABLE IF EXISTS entity_edges CASCADE;
DROP TABLE IF EXISTS entities CASCADE;

-- Vector columns elsewhere (embeddings live in SurrealDB)
ALTER TABLE investigations DROP COLUMN IF EXISTS summary_embedding;
ALTER TABLE agent_skills DROP COLUMN IF EXISTS embedding;
ALTER TABLE investigation_memory DROP COLUMN IF EXISTS embedding;
ALTER TABLE agent_memory_episodic DROP COLUMN IF EXISTS embedding;
ALTER TABLE mitre_techniques DROP COLUMN IF EXISTS embedding;
ALTER TABLE investigation_fingerprints DROP COLUMN IF EXISTS embedding;

-- Extension (safe after columns removed)
DROP EXTENSION IF EXISTS vector CASCADE;

-- Ensure dedup hash index remains (065) — no-op if already present
CREATE INDEX IF NOT EXISTS idx_agent_tasks_dedup_hash ON agent_tasks (dedup_hash)
  WHERE dedup_hash IS NOT NULL;

COMMENT ON SCHEMA public IS 'Ticket 068: entity graph + pgvector retired; SurrealDB + DuckDB per data-plane contract.';
