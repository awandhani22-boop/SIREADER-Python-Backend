-- Rollback v2 to the Phase 5.2.2 v1 activity policy (no TASK_RETRIED command event).
-- Use only on the same explicitly named disposable/test targets.
BEGIN;
DO $guard$
DECLARE db_owner text;
BEGIN
  IF current_database() NOT IN ('sireader_disposable','sireader_test','sireader_ci') THEN RAISE EXCEPTION 'Disposable/test DB only'; END IF;
  IF current_database()='sireader_disposable' AND inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN RAISE EXCEPTION 'Loopback only'; END IF;
  SELECT r.rolname INTO db_owner FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database();
  IF db_owner NOT IN ('sireader_dev','postgres') OR current_user NOT IN ('sireader_dev','postgres') THEN RAISE EXCEPTION 'Database owner required'; END IF;
END;
$guard$;
DROP POLICY IF EXISTS workforce_cmd_activity_insert ON public.workforce_activity_events;
CREATE POLICY workforce_cmd_activity_insert
ON public.workforce_activity_events FOR INSERT TO sireader_workforce_cmd
WITH CHECK (
  (event_type='WORKER_STATE_CHANGED' AND worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist') AND task_id IS NULL)
  OR
  (event_type IN ('TASK_ENQUEUED','TASK_CANCELLED','TASK_WAITING_REVIEW') AND task_id IS NOT NULL
   AND (worker_id IS NULL OR worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
   AND EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.task_id=workforce_activity_events.task_id))
  OR
  (event_type IN ('HUMAN_REVIEW_APPROVED','HUMAN_REVIEW_EDITED','HUMAN_REVIEW_REJECTED')
   AND actor_type='OPERATOR' AND actor_id='local_operator' AND task_id IS NOT NULL
   AND (worker_id IS NULL OR worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
   AND EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.task_id=workforce_activity_events.task_id))
);
COMMIT;
