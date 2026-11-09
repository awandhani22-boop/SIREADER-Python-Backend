-- SIREADER Phase 5.2.2-B v2: validate the first row image of quarantined review payloads.
-- Required for databases that already applied workforce_reviews_v1.
BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.workforce_task_reviews') IS NULL
       OR to_regclass('public.workforce_task_review_payloads') IS NULL
       OR to_regprocedure('public.sireader_guard_review_payloads_v1()') IS NULL THEN
        RAISE EXCEPTION 'Apply workforce_reviews_v1 before workforce_review_payload_insert_guard_v2';
    END IF;
END;
$guard$;

CREATE OR REPLACE FUNCTION public.sireader_guard_review_payloads_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    r_status varchar(24);
    t_expires_at timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_review_payloads records cannot be deleted; use retention purge';
    END IF;

    -- A newly-created payload row may contain the original quarantine only.
    -- An adjusted value can enter storage only after an EDITED adjudication.
    IF TG_OP = 'INSERT' THEN
        SELECT status INTO r_status
        FROM public.workforce_task_reviews
        WHERE review_id = NEW.review_id;

        IF r_status IS DISTINCT FROM 'PENDING' THEN
            RAISE EXCEPTION 'Initial review payload requires a PENDING review (current status: %)', r_status;
        END IF;
        IF NEW.original_output IS NULL
           OR NEW.adjusted_output IS NOT NULL
           OR NEW.payload_purged_at IS NOT NULL THEN
            RAISE EXCEPTION 'Initial review payload may contain only original_output; adjusted_output and purge marker must be NULL';
        END IF;

        NEW.updated_at = clock_timestamp();
        RETURN NEW;
    END IF;

    -- Anti-resurrection: once purged, cannot modify or restore.
    IF OLD.payload_purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'Purged review payload cannot be modified or restored.';
    END IF;

    -- Purge operation validation.
    IF NEW.payload_purged_at IS NOT NULL THEN
        IF NEW.original_output IS NOT NULL OR NEW.adjusted_output IS NOT NULL THEN
            RAISE EXCEPTION 'Purging requires original_output and adjusted_output to be NULL.';
        END IF;

        SELECT r.status, t.payload_expires_at INTO r_status, t_expires_at
        FROM public.workforce_task_reviews r
        JOIN public.workforce_tasks t ON t.task_id = r.task_id
        WHERE r.review_id = OLD.review_id;

        IF r_status NOT IN ('APPROVED', 'EDITED', 'REJECTED', 'EXPIRED') THEN
            RAISE EXCEPTION 'Cannot purge payloads for active review % in status %.', OLD.review_id, r_status;
        END IF;

        IF t_expires_at IS NULL OR t_expires_at > clock_timestamp() THEN
            RAISE EXCEPTION 'Cannot purge review payload before parent task expiration (expires at: %).', t_expires_at;
        END IF;

        -- Database independently timestamps the purge; callers cannot pass arbitrary timestamp.
        NEW.payload_purged_at = clock_timestamp();
    ELSE
        -- Not purging: original_output is strictly immutable.
        IF OLD.original_output IS NOT NULL AND NEW.original_output IS DISTINCT FROM OLD.original_output THEN
            RAISE EXCEPTION 'Original review output is immutable.';
        END IF;

        -- adjusted_output is write-once and can ONLY be introduced when review is EDITED.
        IF OLD.adjusted_output IS NOT NULL AND NEW.adjusted_output IS DISTINCT FROM OLD.adjusted_output THEN
            RAISE EXCEPTION 'Adjusted review output is write-once.';
        END IF;

        IF OLD.adjusted_output IS NULL AND NEW.adjusted_output IS NOT NULL THEN
            SELECT status INTO r_status FROM public.workforce_task_reviews WHERE review_id = OLD.review_id;
            IF r_status IS DISTINCT FROM 'EDITED' THEN
                RAISE EXCEPTION 'Adjusted review output can only be written for reviews in EDITED status (current status: %)', r_status;
            END IF;
        END IF;
    END IF;

    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workforce_task_review_payloads_guard ON public.workforce_task_review_payloads;
CREATE TRIGGER trg_workforce_task_review_payloads_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.workforce_task_review_payloads
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_payloads_v1();

COMMIT;
