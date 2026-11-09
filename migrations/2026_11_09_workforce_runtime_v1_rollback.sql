-- SIREADER Phase 4.3 workforce runtime rollback. Run only with database-owner approval.
BEGIN;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_workers;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_workers;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_capabilities;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_capabilities;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_worker_capabilities;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_worker_capabilities;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_heartbeats;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_heartbeats;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_tasks;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_tasks;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_task_attempts;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_task_attempts;
DROP TRIGGER IF EXISTS sireader_workforce_notify ON public.workforce_activity_events;
DROP TRIGGER IF EXISTS sireader_workforce_truncate ON public.workforce_activity_events;
DROP FUNCTION IF EXISTS public.sireader_notify_workforce_v1();
DROP TABLE IF EXISTS public.workforce_activity_events;
DROP TABLE IF EXISTS public.workforce_task_attempts;
DROP TABLE IF EXISTS public.workforce_tasks;
DROP TABLE IF EXISTS public.workforce_heartbeats;
DROP TABLE IF EXISTS public.workforce_worker_capabilities;
DROP TABLE IF EXISTS public.workforce_capabilities;
DROP TABLE IF EXISTS public.workforce_workers;
COMMIT;

-- Intentionally does not drop shared roles or the Phase 3.3 observability channel/function.