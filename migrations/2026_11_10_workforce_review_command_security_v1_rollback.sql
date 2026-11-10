-- Rollback for workforce_review_command_security_v1.sql.
-- Deliberately keeps RLS enabled on review tables with no command policies (deny by default).
BEGIN;
DO $guard$
BEGIN
    IF current_database() NOT IN ('sireader_disposable', 'sireader_test', 'sireader_ci')
       OR (current_database() = 'sireader_disposable'
           AND inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet) THEN
        RAISE EXCEPTION 'Rollback is restricted to the explicitly named disposable/test targets';
    END IF;
END;
$guard$;

DROP TRIGGER IF EXISTS trg_sireader_guard_workforce_result_release ON public.workforce_tasks;
DROP FUNCTION IF EXISTS public.sireader_guard_workforce_result_release_v1();
REVOKE UPDATE (result_payload) ON public.workforce_tasks FROM sireader_workforce_cmd;

DROP POLICY IF EXISTS workforce_cmd_task_reviews_select ON public.workforce_task_reviews;
DROP POLICY IF EXISTS workforce_cmd_task_reviews_update ON public.workforce_task_reviews;
DROP POLICY IF EXISTS workforce_cmd_review_payloads_select ON public.workforce_task_review_payloads;
DROP POLICY IF EXISTS workforce_cmd_review_payloads_update ON public.workforce_task_review_payloads;

DROP POLICY IF EXISTS workforce_cmd_activity_insert ON public.workforce_activity_events;
CREATE POLICY workforce_cmd_activity_insert
ON public.workforce_activity_events FOR INSERT TO sireader_workforce_cmd
WITH CHECK (
    (event_type = 'WORKER_STATE_CHANGED'
     AND worker_id IN ('supervisor', 'haiku', 'sonnet', 'opus', 'data_archivist')
     AND task_id IS NULL)
    OR
    (event_type IN ('TASK_ENQUEUED', 'TASK_CANCELLED', 'TASK_WAITING_REVIEW')
     AND task_id IS NOT NULL
     AND (worker_id IS NULL OR worker_id IN ('supervisor', 'haiku', 'sonnet', 'opus', 'data_archivist'))
     AND EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.task_id = workforce_activity_events.task_id))
);

-- RLS remains enabled and review command access is denied until the forward migration is reapplied.
COMMIT;
