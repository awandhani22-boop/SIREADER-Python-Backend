-- SIREADER Phase 5.2.2 — close the database security boundary for human review adjudication.
-- Applies only to the loopback disposable PostgreSQL databases used by local/CI acceptance.
-- Never apply to Supabase LIVE or production.
BEGIN;

DO $guard$
DECLARE
    db_owner text;
BEGIN
    IF current_database() NOT IN ('sireader_disposable', 'sireader_test', 'sireader_ci') THEN
        RAISE EXCEPTION 'This migration is restricted to explicitly named disposable/test databases';
    END IF;
    IF current_database() = 'sireader_disposable'
       AND inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN
        RAISE EXCEPTION 'The local disposable database must be reached through loopback';
    END IF;
    SELECT r.rolname INTO db_owner
    FROM pg_catalog.pg_database d
    JOIN pg_catalog.pg_roles r ON r.oid = d.datdba
    WHERE d.datname = current_database();
    IF db_owner NOT IN ('sireader_dev', 'postgres') OR current_user NOT IN ('sireader_dev', 'postgres') THEN
        RAISE EXCEPTION 'This migration requires the disposable database owner';
    END IF;
    IF to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_activity_events') IS NULL
       OR to_regclass('public.workforce_task_reviews') IS NULL
       OR to_regclass('public.workforce_task_review_payloads') IS NULL THEN
        RAISE EXCEPTION 'Required Phase 5 workforce/review tables are missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_workforce_cmd') THEN
        RAISE EXCEPTION 'Dedicated sireader_workforce_cmd role is missing';
    END IF;
END;
$guard$;

-- Review metadata and quarantined payloads are command-readable only through
-- row-level policies tied to a task visible to the non-trading command role.
ALTER TABLE public.workforce_task_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_task_review_payloads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workforce_cmd_task_reviews_select ON public.workforce_task_reviews;
CREATE POLICY workforce_cmd_task_reviews_select
ON public.workforce_task_reviews FOR SELECT TO sireader_workforce_cmd
USING (
    status IN ('PENDING', 'APPROVED', 'EDITED', 'REJECTED', 'EXPIRED')
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_reviews.task_id
    )
);

DROP POLICY IF EXISTS workforce_cmd_task_reviews_update ON public.workforce_task_reviews;
CREATE POLICY workforce_cmd_task_reviews_update
ON public.workforce_task_reviews FOR UPDATE TO sireader_workforce_cmd
USING (
    status = 'PENDING'
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_reviews.task_id
    )
)
WITH CHECK (
    status IN ('APPROVED', 'EDITED', 'REJECTED')
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_reviews.task_id
    )
);

DROP POLICY IF EXISTS workforce_cmd_review_payloads_select ON public.workforce_task_review_payloads;
CREATE POLICY workforce_cmd_review_payloads_select
ON public.workforce_task_review_payloads FOR SELECT TO sireader_workforce_cmd
USING (
    EXISTS (
        SELECT 1 FROM public.workforce_task_reviews r
        WHERE r.review_id = workforce_task_review_payloads.review_id
    )
);

-- An adjusted payload can be written only after its metadata row has moved to
-- EDITED in the same transaction. No INSERT or DELETE policy is granted.
DROP POLICY IF EXISTS workforce_cmd_review_payloads_update ON public.workforce_task_review_payloads;
CREATE POLICY workforce_cmd_review_payloads_update
ON public.workforce_task_review_payloads FOR UPDATE TO sireader_workforce_cmd
USING (
    EXISTS (
        SELECT 1 FROM public.workforce_task_reviews r
        WHERE r.review_id = workforce_task_review_payloads.review_id
          AND r.status = 'EDITED'
    )
)
WITH CHECK (
    EXISTS (
        SELECT 1 FROM public.workforce_task_reviews r
        WHERE r.review_id = workforce_task_review_payloads.review_id
          AND r.status = 'EDITED'
    )
);

-- The command role's original event policy predates the adjudication route.
-- Extend it only for authenticated local-operator review outcomes.
DROP POLICY IF EXISTS workforce_cmd_activity_insert ON public.workforce_activity_events;
CREATE POLICY workforce_cmd_activity_insert
ON public.workforce_activity_events FOR INSERT TO sireader_workforce_cmd
WITH CHECK (
    (
        event_type = 'WORKER_STATE_CHANGED'
        AND worker_id IN ('supervisor', 'haiku', 'sonnet', 'opus', 'data_archivist')
        AND task_id IS NULL
    )
    OR
    (
        event_type IN ('TASK_ENQUEUED', 'TASK_CANCELLED', 'TASK_WAITING_REVIEW')
        AND task_id IS NOT NULL
        AND (worker_id IS NULL OR worker_id IN ('supervisor', 'haiku', 'sonnet', 'opus', 'data_archivist'))
        AND EXISTS (
            SELECT 1 FROM public.workforce_tasks t
            WHERE t.task_id = workforce_activity_events.task_id
        )
    )
    OR
    (
        event_type IN ('HUMAN_REVIEW_APPROVED', 'HUMAN_REVIEW_EDITED', 'HUMAN_REVIEW_REJECTED')
        AND actor_type = 'OPERATOR'
        AND actor_id = 'local_operator'
        AND task_id IS NOT NULL
        AND (worker_id IS NULL OR worker_id IN ('supervisor', 'haiku', 'sonnet', 'opus', 'data_archivist'))
        AND EXISTS (
            SELECT 1 FROM public.workforce_tasks t
            WHERE t.task_id = workforce_activity_events.task_id
        )
    )
);

-- The adjudication service releases approved/edited content into task.result_payload.
-- Grant only this column; the trigger verifies that the value is exactly the
-- quarantined original or the write-once edited payload for a terminal review.
GRANT UPDATE (result_payload) ON public.workforce_tasks TO sireader_workforce_cmd;

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

    -- Retention may clear a result only after its parent payload expiry and
    -- only when it records a purge marker. No other clearing is allowed.
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

DROP TRIGGER IF EXISTS trg_sireader_guard_workforce_result_release ON public.workforce_tasks;
CREATE TRIGGER trg_sireader_guard_workforce_result_release
BEFORE UPDATE OF result_payload ON public.workforce_tasks
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_workforce_result_release_v1();

COMMIT;
