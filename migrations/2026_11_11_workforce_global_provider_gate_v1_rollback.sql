-- Rollback Phase 7.4 provider gate function and its execution grant.
BEGIN;
DO $guard$
BEGIN
    IF current_database() <> 'sireader_test' THEN
        RAISE EXCEPTION 'Phase 7.4 provider gate rollback is restricted to database sireader_test';
    END IF;
END;
$guard$;
DROP FUNCTION IF EXISTS public.sireader_provider_slot_available_v1(integer);
COMMIT;
