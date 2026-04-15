-- =============================================================================
-- migrations/seed_dev_data.sql
--
-- Deterministic dev/CI fixture data. Inserts one tenant and two users so every
-- tool that assumes "admin@test.local / TestPass2026" works on a fresh boot.
--
-- This file runs automatically on postgres' first-boot via the volume mount
-- in docker-compose.yml at /docker-entrypoint-initdb.d/02-seed-dev.sql, AFTER
-- 01-init.sql has created the tables. It is also safe to run manually at any
-- time via scripts/seed_dev.sh — every INSERT uses ON CONFLICT DO NOTHING.
--
-- RESERVED UUIDs (DO NOT REUSE for real tenants/users):
--   00000000-0000-0000-0000-000000000001  SYSTEM tenant (migration 063)
--   00000000-0000-0000-0000-000000000010  zovark-dev tenant (this file)
--   00000000-0000-0000-0000-000000000020  admin@test.local (this file)
--   00000000-0000-0000-0000-000000000021  analyst2@test.local (this file)
--   00000000-0000-0000-0000-0000000000FF  (upper bound of the reserved range)
--
-- Password for both fixture users: TestPass2026
-- Hash algorithm: bcrypt cost 12 ($2b$12$…). To rotate, run:
--     scripts/seed_dev.sh --regenerate-hash
-- The script emits a ready-to-paste UPDATE snippet AND reminds you to update
-- the literal hash in this file so fresh boots pick up the new value.
--
-- Idempotency: every INSERT uses ON CONFLICT (id) DO NOTHING so running this
-- file twice is a no-op. Safe to invoke from cron, from CI, or manually.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------- Dev tenant
INSERT INTO tenants (id, name, slug, tier, is_active, created_at, updated_at)
VALUES (
    '00000000-0000-0000-0000-000000000010',
    'Zovark Dev',
    'zovark-dev',
    'enterprise',
    true,
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------- Admin user
-- Password: TestPass2026 (bcrypt cost 12)
INSERT INTO users (
    id, tenant_id, email, display_name, password_hash, role, is_active,
    created_at, updated_at
)
VALUES (
    '00000000-0000-0000-0000-000000000020',
    '00000000-0000-0000-0000-000000000010',
    'admin@test.local',
    'Admin',
    '$2b$12$qRPF.Bpe.uRqcCxcJAKcWu1aGVz3uBOoOs7zTR2AjPqRtTllnNw7K',
    'admin',
    true,
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------- Analyst user
-- Password: TestPass2026 (bcrypt cost 12) — same literal as admin for dev
-- convenience. Role is 'analyst' for tests that need a non-admin fixture.
INSERT INTO users (
    id, tenant_id, email, display_name, password_hash, role, is_active,
    created_at, updated_at
)
VALUES (
    '00000000-0000-0000-0000-000000000021',
    '00000000-0000-0000-0000-000000000010',
    'analyst2@test.local',
    'Analyst 2',
    '$2b$12$qRPF.Bpe.uRqcCxcJAKcWu1aGVz3uBOoOs7zTR2AjPqRtTllnNw7K',
    'analyst',
    true,
    NOW(),
    NOW()
)
ON CONFLICT (id) DO NOTHING;

COMMIT;
