-- Migration 070: probe_writes diagnostic table
-- Used by POST /api/v1/admin/diagnostics/probe-db to exercise the parameterised
-- INSERT...RETURNING + DELETE roundtrip that catches pgx<->PgBouncer prepared
-- statement collisions (SQLSTATE 08P01). Not a tenant table — global diagnostic
-- only. No RLS. The handler always inserts and immediately deletes the row in
-- the same transaction, so the table should remain empty in steady state.

BEGIN;

CREATE TABLE IF NOT EXISTS probe_writes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT INSERT, DELETE, SELECT ON probe_writes TO zovark;

COMMIT;
