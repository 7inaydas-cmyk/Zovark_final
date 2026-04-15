-- Migration 072: schema_migrations ledger + init.sql-era backfill
--
-- WHAT: Creates the schema_migrations ledger table that records which
-- migrations/*.sql files have been applied to this Postgres database, and
-- backfills it with the 60 init.sql-era filenames (000_* through 055_*,
-- including the four duplicate-numbered files at 041 / 050 / 051 / 052).
--
-- WHY: The Zovark dev Postgres has historically had no migration tracking.
-- init.sql is mounted into /docker-entrypoint-initdb.d/ and runs once on a
-- fresh volume, leaving the DB at a frozen state that approximates "migrations
-- 001-053 hand-merged at some past date". Subsequent migrations (054 onward)
-- are operator-applied with `psql < f` and there is no record of which ones
-- ran. The Phase 1 audit on branch audit/execution-fixes (2026-04-13) found
-- 14 migrations on disk that were never applied, with the API silently
-- depending on columns from each of them.
--
-- The init.sql-era backfill timestamps are sentinel (2026-01-01T00:00:00Z) —
-- we do not know when these migrations actually ran (they didn't, formally)
-- so a date that's obviously not a real applied_at is more honest than
-- now(). An operator querying `applied_at < '2026-04-13'` can find the entire
-- frozen era in one predicate.
--
-- IDEMPOTENT: CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING.
-- Re-running this migration is a no-op.
--
-- See: docs/RUNBOOK_HEALTHCHECK.md#schema-drift
-- See: scripts/apply_migrations.sh

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename     TEXT        PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by   TEXT        NOT NULL DEFAULT current_user,
    source       TEXT        NOT NULL CHECK (source IN ('init_sql', 'migration_runner', 'manual_backfill')),
    checksum     TEXT
);

COMMENT ON TABLE schema_migrations IS
  'Audit trail of applied migration files. Filename is the canonical key; '
  'source distinguishes files baked into init.sql vs run by the migration '
  'runner vs marked manually after operator-applied psql.';

-- Backfill the init.sql-frozen era (58 files: 54 unique numbers 000-053 + 4 duplicate-numbered at 041/050/051/052).
-- Generated 2026-04-13 by `ls migrations/*.sql | sort -V | awk '$1 ≤ 053'`.
-- NOTE: 054_cipher_audit_events.sql and 055_template_promotion.sql are NOT in this list —
-- the Phase 1 audit confirmed neither cipher_audit_events nor template_promotion exist in
-- the dev DB, so init.sql does NOT bake them in. Both will be applied by scripts/apply_migrations.sh.
INSERT INTO schema_migrations (filename, applied_at, applied_by, source, checksum) VALUES
    ('000_init_agent_tasks.sql',                          '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('001_sprint1g_entity_graph.sql',                     '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('002_schema_drift_fixes.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('003_sprint1e_hardening.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('004_sprint1f_bootstrap.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('005_sprint1l_golden_path.sql',                      '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('006_sprint1k_cross_tenant.sql',                     '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('007_sprint1i_model_tiering.sql',                    '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('008_sprint2a_detection_engine.sql',                 '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('009_sprint2b_soar_playbooks.sql',                   '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('010_sprint3c_tenant_webhooks.sql',                  '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('011_sprint3d_finetuning.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('012_sprint3e_model_registry.sql',                   '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('013_sprint3f_security.sql',                         '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('014_sprint4a_sre_agent.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('015_sprint5_seed_skills_and_validation.sql',        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('016_alert_fingerprints.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('017_investigation_feedback.sql',                    '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('018_cost_tracking.sql',                             '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('019_investigation_cache.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('020_failure_context.sql',                           '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('021_hnsw_indexes.sql',                              '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('022_api_keys.sql',                                  '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('023_totp.sql',                                      '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('024_scheduled_workflows.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('025_incidents.sql',                                 '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('026_sla_events.sql',                                '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('027_shadow_mode.sql',                               '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('028_token_quotas.sql',                              '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('029_kill_switch.sql',                               '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('030_pii_detection.sql',                             '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('031_nats_streams.sql',                              '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('032_stampede_protection.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('033_p1_tenant_isolation.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('034_column_encryption.sql',                         '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('035_row_level_security.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('036_vault_integration.sql',                         '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('037_performance_indexes.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('038_kev_processing.sql',                            '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('039_drop_legacy_tables.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('040_human_review_flags.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('041_network_beaconing_skill.sql',                   '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('041_system_configs.sql',                            '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('042_investigation_fingerprints.sql',                '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('043_investigations_merged_context.sql',             '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('044_investigations_dedup_columns.sql',              '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('045_investigation_memory.sql',                      '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('046_llm_audit_log.sql',                             '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('047_add_model_name.sql',                            '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('048_threat_type_aliases.sql',                       '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('049_sprint1e_hardening.sql',                        '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('050_model_performance_tracking.sql',                '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('050_sprint1k_cross_tenant_entities.sql',            '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('051_bootstrap_pipeline_enhancements.sql',           '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('051_sprint2a_detection_rules_enhancements.sql',     '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('052_rate_limit_audit.sql',                          '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('052_sprint2b_soar_playbooks_enhancements.sql',      '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL),
    ('053_ioc_evidence_refs.sql',                         '2026-01-01T00:00:00Z', 'init_sql_freeze_2026-04-13', 'init_sql', NULL)
ON CONFLICT (filename) DO NOTHING;

COMMIT;
