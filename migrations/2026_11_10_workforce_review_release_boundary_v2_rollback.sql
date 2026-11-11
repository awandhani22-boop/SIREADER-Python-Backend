-- Rollback Phase 7.2 acceptance fix; restores the stricter review-only result release guard.
-- Restricted to explicitly named disposable/test databases. Never apply to Supabase LIVE.
BEGIN;

DO $guard$
BEGIN
    IF current_database() NOT IN ('sireader_disposable', 'sireader_test', 'sireader_ci') THEN
        RAISE EXCEPTION 'This rollback is restricted to explicitly named disposable/test databases';
    END IF;
    IF to_regprocedure('public.sireader_guard_workforce_result_release_v1()') IS NULL THEN
        RAISE EXCEPTION 'The workforce result release guard does not exist';
    END IF;
END;
$guard$;

CREATE OR REPLACE FUNCTION public.sireader_guard_workforce_result_release_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    review_status text;
    original_value jsonb;
    adjusted_value jsonb;
    payload_purged_at timestamptz;
BEGIN
    IF NEW.result_payload IS NOT DISTINCT FROM OLD.result_payload THEN
        RETURN NEW;
    END IF;

    IF NEW.result_payload IS NULL THEN
        IF OLD.result_payload IS NOT NULL
           AND NEW.payload_purged_at IS NOT NULL
           AND NEW.payload_expires_at IS NOT NULL
           AND NEW.payload_expires_at <= clock_timestamp() THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'SIREADER result payload may only be cleared by the retention boundary';
    END IF;

    IF OLD.result_payload IS NOT NULL THEN
        RAISE EXCEPTION 'SIREADER result payload release is write-once';
    END IF;
    IF NEW.status <> 'COMPLETED' THEN
        RAISE EXCEPTION 'SIREADER result payload may only be released for a completed task';
    END IF;

    SELECT r.status, p.original_output, p.adjusted_output, p.payload_purged_at
    INTO review_status, original_value, adjusted_value, payload_purged_at
    FROM public.workforce_task_reviews r
    JOIN public.workforce_task_review_payloads p ON p.review_id = r.review_id
    WHERE r.task_id = NEW.task_id;

    IF NOT FOUND OR payload_purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'SIREADER result payload release requires an unpurged review record';
    END IF;

    IF review_status = 'APPROVED' AND original_value IS NOT NULL
       AND NEW.result_payload IS NOT DISTINCT FROM original_value THEN
        RETURN NEW;
    END IF;
    IF review_status = 'EDITED' AND adjusted_value IS NOT NULL
       AND NEW.result_payload IS NOT DISTINCT FROM adjusted_value THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'SIREADER result payload must match the approved or edited quarantined output';
END;
$function$;

COMMIT;
