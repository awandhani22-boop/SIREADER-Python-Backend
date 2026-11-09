-- Phase 5.2.3: read-only workforce cost attribution over the existing Cost Guard ledger.
BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.ai_usage_logs') IS NULL
       OR to_regclass('public.workforce_task_attempts') IS NULL THEN
        RAISE EXCEPTION 'Cost observability requires ai_usage_logs and workforce_task_attempts';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required SIREADER API roles are missing';
    END IF;
END;
$guard$;

CREATE OR REPLACE VIEW public.v_api_workforce_ai_usage
WITH (security_barrier = true) AS
SELECT
    u.usage_date,
    a.worker_id,
    a.task_id,
    a.attempt_id,
    u.provider,
    u.model,
    u.tier,
    u.billing_currency,
    u.cost_rp,
    u.reserved_rp,
    u.cost_usd,
    u.reserved_usd,
    u.status
FROM public.ai_usage_logs AS u
JOIN public.workforce_task_attempts AS a
  ON a.provider_request_id = u.request_id
WHERE a.provider_request_id IS NOT NULL;

GRANT USAGE ON SCHEMA public TO sireader_view_owner;
GRANT SELECT (request_id) ON public.ai_usage_logs TO sireader_view_owner;
GRANT SELECT (provider_request_id) ON public.workforce_task_attempts TO sireader_view_owner;
GRANT CREATE ON SCHEMA public TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_ai_usage OWNER TO sireader_view_owner;
REVOKE CREATE ON SCHEMA public FROM sireader_view_owner;

REVOKE ALL ON public.v_api_workforce_ai_usage FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sireader_api_ro;
GRANT SELECT ON public.v_api_workforce_ai_usage TO sireader_api_ro;

COMMIT;
