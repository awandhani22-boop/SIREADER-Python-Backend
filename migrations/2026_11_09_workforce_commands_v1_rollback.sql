-- SIREADER Phase 5.1 command boundary rollback.
-- Leaves the additive accepting_tasks column/view field in place because
-- the worker scheduler model may already depend on it; command API stays fail-closed.
BEGIN;
DROP TRIGGER IF EXISTS sireader_command_audit_immutable ON public.workforce_command_audit;
DROP FUNCTION IF EXISTS public.sireader_reject_command_audit_mutation_v1();
DROP POLICY IF EXISTS workforce_cmd_audit_select ON public.workforce_command_audit;
DROP POLICY IF EXISTS workforce_cmd_audit_insert ON public.workforce_command_audit;
DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_workers;
DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_workers;
DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_capabilities;
DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_worker_capabilities;
DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_tasks;
DROP POLICY IF EXISTS workforce_cmd_insert ON public.workforce_tasks;
DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_tasks;
DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_task_attempts;
DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_task_attempts;
DROP POLICY IF EXISTS workforce_cmd_insert ON public.workforce_activity_events;
REVOKE ALL ON public.workforce_command_audit FROM sireader_workforce_cmd;
REVOKE ALL ON public.workforce_workers, public.workforce_capabilities,
    public.workforce_worker_capabilities, public.workforce_tasks,
    public.workforce_task_attempts, public.workforce_activity_events
    FROM sireader_workforce_cmd;
DROP TABLE IF EXISTS public.workforce_command_audit;
COMMIT;
