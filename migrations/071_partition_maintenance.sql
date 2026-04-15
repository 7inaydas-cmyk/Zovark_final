-- Migration 071: Partition maintenance for `investigations` and `audit_events`
--
-- Audit 3.22: the monthly partitions in init.sql only ran through 2026-12, with
-- a `_default` partition as a catch-all. Past that date, every insert lands in
-- the default partition, queries with `WHERE created_at > now() - '30 days'`
-- degrade to a default-partition full scan, and you cannot ATTACH new
-- partitions without first DETACHing the default (blocking maintenance).
--
-- This migration:
--   1. Installs `create_next_month_partition()` — a pure-plpgsql function that
--      creates the next month's partition for a parent table if it does not
--      already exist.
--   2. Pre-creates monthly partitions for `investigations` and `audit_events`
--      through 2028-12 so the current deployment has two years of headroom.
--   3. Adds a small maintenance entrypoint `maintain_zovark_partitions()` that
--      operators (or a k8s CronJob) can call daily to keep the rolling window
--      12 months ahead of `now()`.
--
-- Idempotent: every CREATE / ATTACH is guarded by IF NOT EXISTS checks.
-- Does NOT install pg_cron or pg_partman (those are optional — operators may
-- choose to invoke `maintain_zovark_partitions()` from any cron they prefer).

BEGIN;

-- ---------------------------------------------------------------------------
-- Helper: create one monthly partition for a given parent table if missing.
-- Partition bounds follow the half-open interval convention
-- [YYYY-MM-01, YYYY-MM+1-01).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION create_month_partition(
    parent_table  text,
    year_month    date
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    part_name  text;
    start_bound text;
    end_bound   text;
BEGIN
    part_name := format('%I_%s', parent_table, to_char(year_month, 'YYYY_MM'));
    start_bound := to_char(date_trunc('month', year_month), 'YYYY-MM-DD');
    end_bound   := to_char(date_trunc('month', year_month) + INTERVAL '1 month', 'YYYY-MM-DD');

    IF NOT EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = part_name
          AND n.nspname = current_schema()
    ) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            part_name, parent_table, start_bound, end_bound
        );
        RAISE NOTICE 'created partition %', part_name;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Helper: create the next N monthly partitions for a parent table starting
-- from month_offset (0 = current month).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION create_next_month_partitions(
    parent_table  text,
    months_ahead  int DEFAULT 12
) RETURNS int
LANGUAGE plpgsql
AS $$
DECLARE
    i int;
    created int := 0;
    target date;
BEGIN
    FOR i IN 0..months_ahead LOOP
        target := date_trunc('month', NOW())::date + (i || ' months')::interval;
        BEGIN
            PERFORM create_month_partition(parent_table, target);
            created := created + 1;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'partition creation failed for %: %', parent_table, SQLERRM;
        END;
    END LOOP;
    RETURN created;
END;
$$;

-- ---------------------------------------------------------------------------
-- Operator entrypoint: ensure every tenant-scoped partitioned table has at
-- least 12 monthly partitions in the rolling window [now, now + 12 months].
-- Call daily from a k8s CronJob or a Postgres pg_cron schedule.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION maintain_zovark_partitions()
RETURNS TABLE(parent text, created int)
LANGUAGE plpgsql
AS $$
DECLARE
    tables text[] := ARRAY['investigations', 'audit_events'];
    t text;
    n int;
BEGIN
    FOREACH t IN ARRAY tables LOOP
        -- Only operate on partitioned tables; silently skip missing/plain tables.
        IF EXISTS (
            SELECT 1 FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = t
        ) THEN
            SELECT create_next_month_partitions(t, 12) INTO n;
            parent := t;
            created := n;
            RETURN NEXT;
        END IF;
    END LOOP;
    RETURN;
END;
$$;

-- ---------------------------------------------------------------------------
-- Eager pre-creation: run through 2028-12 for both tables. Safe to re-run —
-- every create is guarded. This gives the current deployment two years of
-- runway even if the daily maintenance job never lands.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
    y int;
    m int;
    target date;
BEGIN
    FOR t IN SELECT unnest(ARRAY['investigations', 'audit_events']) LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = t
        ) THEN
            CONTINUE;
        END IF;
        FOR y IN 2026..2028 LOOP
            FOR m IN 1..12 LOOP
                target := make_date(y, m, 1);
                BEGIN
                    PERFORM create_month_partition(t, target);
                EXCEPTION WHEN OTHERS THEN
                    RAISE WARNING 'pre-create % % failed: %', t, target, SQLERRM;
                END;
            END LOOP;
        END LOOP;
    END LOOP;
END;
$$;

COMMIT;
