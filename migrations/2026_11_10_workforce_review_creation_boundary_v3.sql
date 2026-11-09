-- Phase 5.2.2-B v3: reserve review creation for the worker completion boundary.
-- Requires workforce_reviews_v1 and workforce_review_payload_insert_guard_v2.
BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.workforce_task_reviews') IS NULL
       OR to_regclass('public.workforce_task_review_payloads') IS NULL
       OR to_regprocedure('public.sireader_guard_review_metadata_v1()') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_workforce_cmd') THEN
        RAISE EXCEPTION 'Apply workforce_reviews_v1 and provision sireader_workforce_cmd before v3';
    END IF;
END;
$guard$;

CREATE OR REPLACE FUNCTION public.sireader_guard_review_metadata_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_reviews records are permanent and cannot be deleted';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'PENDING'
           OR NEW.adjusted_output_sha256 IS NOT NULL
           OR NEW.adjudicated_by IS NOT NULL
           OR NEW.adjudicated_at IS NOT NULL
           OR NEW.reason_code IS NOT NULL
           OR NEW.notes IS NOT NULL THEN
            RAISE EXCEPTION 'New review metadata must start in PENDING without adjudication fields';
        END IF;
        NEW.updated_at = clock_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.original_output_sha256 IS DISTINCT FROM NEW.original_output_sha256 OR
       OLD.task_id IS DISTINCT FROM NEW.task_id OR
       OLD.attempt_id IS DISTINCT FROM NEW.attempt_id THEN
        RAISE EXCEPTION 'Core governance references and original hash are immutable.';
    END IF;

    IF OLD.status IN ('APPROVED', 'EDITED', 'REJECTED', 'EXPIRED') THEN
        RAISE EXCEPTION 'Terminal review state % cannot be modified.', OLD.status;
    END IF;

    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workforce_task_reviews_guard ON public.workforce_task_reviews;
CREATE TRIGGER trg_workforce_task_reviews_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.workforce_task_reviews
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_metadata_v1();

-- Command API only adjudicates existing reviews. Worker completion owns INSERT.
REVOKE INSERT ON public.workforce_task_reviews FROM sireader_workforce_cmd;
REVOKE INSERT ON public.workforce_task_review_payloads FROM sireader_workforce_cmd;

COMMIT;
