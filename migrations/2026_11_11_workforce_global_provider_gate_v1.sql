-- SIREADER Phase 7.4 — global one-request provider concurrency gate.
-- The existing task-attempt state is the durable slot ledger:
-- PROVIDER_REQUESTED and UNKNOWN consume the sole global slot.
-- UNKNOWN is never cleared by TTL; reconciliation is an explicit operator action.
BEGIN;

DO $guard$
DECLARE
    owner_role pg_catalog.pg_roles%ROWTYPE;
    runtime_role pg_catalog.pg_roles%ROWTYPE;
BEGIN
    IF current_database() <> 'sireader_test' THEN
        RAISE EXCEPTION 'Phase 7.4 provider gate migration is restricted to database sireader_test';
    END IF;

    SELECT * INTO owner_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'sireader_view_owner';
    IF NOT FOUND
       OR owner_role.rolcanlogin
       OR owner_role.rolsuper
       OR owner_role.rolcreatedb
       OR owner_role.rolcreaterole
       OR owner_role.rolreplication
       OR owner_role.rolbypassrls
       OR owner_role.rolinherit THEN
        RAISE EXCEPTION 'sireader_view_owner role is missing or violates least privilege';
    END IF;

    SELECT * INTO runtime_role
    FROM pg_catalog.pg_roles
    WHERE rolname = 'postgres';
    IF NOT FOUND OR NOT runtime_role.rolcanlogin THEN
        RAISE EXCEPTION 'The existing postgres worker-runtime login role is required';
    END IF;

    IF NOT pg_catalog.has_column_privilege(
        'sireader_view_owner',
        'public.workforce_task_attempts',
        'status',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'sireader_view_owner must have SELECT on attempt status';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'workforce_task_attempts'
          AND policyname = 'workforce_view_owner_select'
    ) THEN
        RAISE EXCEPTION 'workforce_task_attempts must retain its view-owner RLS policy';
    END IF;
END;
$guard$;

CREATE OR REPLACE FUNCTION public.sireader_provider_slot_available_v1(p_limit integer)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    active_requests bigint;
BEGIN
    IF p_limit IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'Phase 7.4 provider concurrency limit is fixed at one';
    END IF;

    -- Serializes reservations across all processes, workers, and hosts sharing this DB.
    PERFORM pg_catalog.pg_advisory_xact_lock(61743092815670031::bigint);

    SELECT pg_catalog.count(*)
      INTO active_requests
      FROM public.workforce_task_attempts
     WHERE status IN ('PROVIDER_REQUESTED', 'UNKNOWN');

    -- Phase 7.4 is intentionally serialized: exactly one provider request globally.
    RETURN active_requests < p_limit;
END;
$function$;

ALTER FUNCTION public.sireader_provider_slot_available_v1(integer)
    OWNER TO sireader_view_owner;

REVOKE ALL ON FUNCTION public.sireader_provider_slot_available_v1(integer)
    FROM PUBLIC, sireader_api_ro;

-- The current worker runtime uses the backend DATABASE_URL identity (postgres
-- on the isolated local canary database). The command API role is deliberately
-- not granted provider-dispatch authority.
GRANT EXECUTE ON FUNCTION public.sireader_provider_slot_available_v1(integer)
    TO postgres;

COMMIT;
