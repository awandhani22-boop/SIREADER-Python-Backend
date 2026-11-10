-- SIREADER Phase 5.2.2 — preserve the non-trading retry audit event under command-role RLS.
-- Applies only to explicitly named disposable/test databases; never Supabase LIVE.
BEGIN;
DO $guard$
DECLARE db_owner text;
BEGIN
  IF current_database() NOT IN ('sireader_disposable','sireader_test','sireader_ci') THEN
    RAISE EXCEPTION 'This migration is restricted to explicitly named disposable/test databases';
  END IF;
  IF current_database()='sireader_disposable' AND inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN
    RAISE EXCEPTION 'The local disposable database must be reached through loopback';
  END IF;
  SELECT r.rolname INTO db_owner FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database();
  IF db_owner NOT IN ('sireader_dev','postgres') OR current_user NOT IN ('sireader_dev','postgres') THEN
    RAISE EXCEPTION 'This migration requires the disposable database owner';
  END IF;
  IF to_regclass('public.workforce_tasks') IS NULL OR to_regclass('public.workforce_activity_events') IS NULL
     OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_workforce_cmd') THEN
    RAISE EXCEPTION 'Required workforce tables or command role are missing';
  END IF;
END;
$guard$;

-- Retry command inserts both TASK_ENQUEUED (new task) and TASK_RETRIED
-- (lineage event on original task). Preserve all earlier command events and
-- the new human-review decisions while requiring both retry endpoints to be
-- visible through the non-trading workforce task RLS policy.
DROP POLICY IF EXISTS workforce_cmd_activity_insert ON public.workforce_activity_events;
CREATE POLICY workforce_cmd_activity_insert
ON public.workforce_activity_events FOR INSERT TO sireader_workforce_cmd
WITH CHECK (
  (
    event_type='WORKER_STATE_CHANGED'
    AND worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist')
    AND task_id IS NULL
  )
  OR
  (
    event_type IN ('TASK_ENQUEUED','TASK_CANCELLED','TASK_WAITING_REVIEW','TASK_RETRIED')
    AND task_id IS NOT NULL
    AND (worker_id IS NULL OR worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.task_id=workforce_activity_events.task_id)
    AND (
      event_type <> 'TASK_RETRIED'
      OR (
        related_task_id IS NOT NULL
        AND EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.task_id=workforce_activity_events.related_task_id)
      )
    )
  )
  OR
  (
    event_type IN ('HUMAN_REVIEW_APPROVED','HUMAN_REVIEW_EDITED','HUMAN_REVIEW_REJECTED')
    AND actor_type='OPERATOR'
    AND actor_id='local_operator'
    AND task_id IS NOT NULL
    AND (worker_id IS NULL OR worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.task_id=workforce_activity_events.task_id)
  )
);
COMMIT;
