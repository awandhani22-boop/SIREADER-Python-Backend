-- Rollback v3 to the v1 metadata trigger and command role's previous grants.
BEGIN;

CREATE OR REPLACE FUNCTION public.sireader_guard_review_metadata_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_reviews records are permanent and cannot be deleted';
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
BEFORE UPDATE OR DELETE ON public.workforce_task_reviews
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_metadata_v1();

GRANT INSERT ON public.workforce_task_reviews TO sireader_workforce_cmd;
GRANT INSERT ON public.workforce_task_review_payloads TO sireader_workforce_cmd;

COMMIT;
