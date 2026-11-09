-- Phase 4.4 read-only workforce view rollback.
BEGIN;
REVOKE ALL ON public.v_api_workforce_workers,
              public.v_api_workforce_tasks,
              public.v_api_workforce_attempts,
              public.v_api_workforce_activity,
              public.v_api_workforce_runtime FROM sireader_api_ro, PUBLIC;
DROP VIEW IF EXISTS public.v_api_workforce_runtime;
DROP VIEW IF EXISTS public.v_api_workforce_activity;
DROP VIEW IF EXISTS public.v_api_workforce_attempts;
DROP VIEW IF EXISTS public.v_api_workforce_tasks;
DROP VIEW IF EXISTS public.v_api_workforce_workers;
COMMIT;
