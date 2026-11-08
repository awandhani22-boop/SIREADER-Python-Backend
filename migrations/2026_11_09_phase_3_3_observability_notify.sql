-- SIREADER Phase 3.3: emit resource invalidations after committed observability writes.
-- Payload is always one static resource key; never include row contents or identifiers.
-- Apply only with the approved database migration owner, never with sireader_api_ro.

CREATE OR REPLACE FUNCTION public.sireader_notify_observability_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    resource_key text;
BEGIN
    IF TG_OP = 'UPDATE' AND to_jsonb(NEW) = to_jsonb(OLD) THEN
        RETURN NULL;
    END IF;

    resource_key := CASE TG_TABLE_NAME
        WHEN 'runtime_control_state' THEN 'system'
        WHEN 'ai_usage_logs' THEN 'telemetry'
        WHEN 'ai_analysis_logs' THEN 'pipeline'
        WHEN 'ai_pending_messages' THEN 'pipeline'
        WHEN 'trade_decision_watches' THEN 'market_watches'
        WHEN 'approval_decisions' THEN 'audit'
        ELSE NULL
    END;

    IF resource_key IS NOT NULL THEN
        PERFORM pg_catalog.pg_notify('sireader_observability_v1', resource_key);
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS sireader_observability_notify
    ON public.runtime_control_state;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.runtime_control_state
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_change();

DROP TRIGGER IF EXISTS sireader_observability_notify
    ON public.ai_usage_logs;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.ai_usage_logs
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_change();

DROP TRIGGER IF EXISTS sireader_observability_notify
    ON public.ai_analysis_logs;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.ai_analysis_logs
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_change();

DROP TRIGGER IF EXISTS sireader_observability_notify
    ON public.ai_pending_messages;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.ai_pending_messages
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_change();

DROP TRIGGER IF EXISTS sireader_observability_notify
    ON public.trade_decision_watches;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.trade_decision_watches
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_change();

-- Approval audit records are append-only: only INSERT creates a new audit event.
DROP TRIGGER IF EXISTS sireader_observability_notify
    ON public.approval_decisions;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT ON public.approval_decisions
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_change();
