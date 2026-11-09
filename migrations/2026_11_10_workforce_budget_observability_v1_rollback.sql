-- Rollback Phase 5.2.3 workforce cost attribution view.
BEGIN;
REVOKE ALL ON public.v_api_workforce_ai_usage FROM PUBLIC;
REVOKE SELECT ON public.v_api_workforce_ai_usage FROM sireader_api_ro;
DROP VIEW IF EXISTS public.v_api_workforce_ai_usage;
COMMIT;
