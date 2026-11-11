-- SIREADER production migration: harden the existing Phase 3.1 read-model views.
-- The existing six views already have the dedicated NOLOGIN view owner and
-- SELECT-only API grants. This migration adds the missing security_barrier
-- option without changing view definitions, ownership, grants, or table data.
--
-- Apply only after reconciling the six existing Phase 3.1 views against the
-- reviewed production baseline. Validate on PostgreSQL 17 staging first.

BEGIN;

DO $guard$
DECLARE
    expected_views constant text[] := ARRAY[
        'v_api_active_in_flight_tasks',
        'v_api_active_market_watches',
        'v_api_audit_decisions',
        'v_api_queued_pending_messages',
        'v_api_system_runtime_state',
        'v_api_telemetry_usage'
    ];
    existing_count integer;
    correct_owner_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'sireader_view_owner'
          AND NOT rolcanlogin
          AND NOT rolsuper
          AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION 'Required least-privilege sireader_view_owner role is missing or unsafe';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'sireader_api_ro'
          AND rolcanlogin
          AND NOT rolsuper
          AND NOT rolbypassrls
    ) THEN
        RAISE EXCEPTION 'Required least-privilege sireader_api_ro role is missing or unsafe';
    END IF;

    SELECT
        count(*)::integer,
        count(*) FILTER (
            WHERE owner_role.rolname = 'sireader_view_owner'
        )::integer
    INTO existing_count, correct_owner_count
    FROM pg_catalog.pg_class AS view_class
    JOIN pg_catalog.pg_namespace AS view_schema
      ON view_schema.oid = view_class.relnamespace
    JOIN pg_catalog.pg_roles AS owner_role
      ON owner_role.oid = view_class.relowner
    WHERE view_schema.nspname = 'public'
      AND view_class.relkind = 'v'
      AND view_class.relname = ANY (expected_views);

    IF existing_count <> cardinality(expected_views)
       OR correct_owner_count <> cardinality(expected_views) THEN
        RAISE EXCEPTION
            'Expected exactly six Phase 3.1 views owned by sireader_view_owner; found %, correctly owned %',
            existing_count, correct_owner_count;
    END IF;
END;
$guard$;

ALTER VIEW public.v_api_active_in_flight_tasks
    SET (security_barrier = true);
ALTER VIEW public.v_api_active_market_watches
    SET (security_barrier = true);
ALTER VIEW public.v_api_audit_decisions
    SET (security_barrier = true);
ALTER VIEW public.v_api_queued_pending_messages
    SET (security_barrier = true);
ALTER VIEW public.v_api_system_runtime_state
    SET (security_barrier = true);
ALTER VIEW public.v_api_telemetry_usage
    SET (security_barrier = true);

DO $verify$
DECLARE
    protected_count integer;
BEGIN
    SELECT count(*)::integer
    INTO protected_count
    FROM pg_catalog.pg_class AS view_class
    JOIN pg_catalog.pg_namespace AS view_schema
      ON view_schema.oid = view_class.relnamespace
    WHERE view_schema.nspname = 'public'
      AND view_class.relkind = 'v'
      AND view_class.relname = ANY (ARRAY[
          'v_api_active_in_flight_tasks',
          'v_api_active_market_watches',
          'v_api_audit_decisions',
          'v_api_queued_pending_messages',
          'v_api_system_runtime_state',
          'v_api_telemetry_usage'
      ])
      AND COALESCE(view_class.reloptions, ARRAY[]::text[])
          @> ARRAY['security_barrier=true'];

    IF protected_count <> 6 THEN
        RAISE EXCEPTION 'Phase 3.1 security-barrier verification failed: expected 6, found %',
            protected_count;
    END IF;
END;
$verify$;

COMMIT;
