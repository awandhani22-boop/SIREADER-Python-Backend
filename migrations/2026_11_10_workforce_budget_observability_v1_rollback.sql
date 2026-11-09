-- Rollback Phase 5.2.3 workforce cost attribution view.
BEGIN;
REVOKE ALL ON public.v_api_workforce_ai_usage FROM PUBLIC;
REVOKE SELECT ON public.v_api_workforce_ai_usage FROM sireader_api_ro;
DROP VIEW IF EXISTS public.v_api_workforce_ai_usage;
REVOKE SELECT (request_id) ON public.ai_usage_logs FROM sireader_view_owner;
REVOKE SELECT (provider_request_id) ON public.workforce_task_attempts FROM sireader_view_owner;
COMMIT;
