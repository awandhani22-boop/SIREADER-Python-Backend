-- SIREADER Phase 3.3: bounded observability invalidation notifications.
-- Apply only through the approved database-owner migration process.
-- The API login sireader_api_ro receives no additional privileges.
-- Payloads contain only fixed resource keys, never row data.

BEGIN;

CREATE OR REPLACE FUNCTION public.sireader_notify_observability_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    resource_key text;
BEGIN
    IF TG_TABLE_SCHEMA <> 'public' THEN
        RAISE EXCEPTION 'SIREADER observability trigger attached outside public schema';
    END IF;

    CASE TG_TABLE_NAME
        WHEN 'runtime_control_state' THEN resource_key := 'system';
        WHEN 'ai_usage_logs' THEN resource_key := 'telemetry';
        WHEN 'ai_analysis_logs' THEN resource_key := 'pipeline';
        WHEN 'ai_pending_messages' THEN resource_key := 'pipeline';
        WHEN 'trade_decision_watches' THEN resource_key := 'market_watches';
        WHEN 'approval_decisions' THEN resource_key := 'audit';
        ELSE
            RAISE EXCEPTION 'Unsupported SIREADER observability trigger source';
    END CASE;

    IF TG_OP = 'TRUNCATE' THEN
        PERFORM pg_catalog.pg_notify('sireader_observability_v1', resource_key);
        RETURN NULL;
    END IF;

    IF TG_OP = 'UPDATE' AND pg_catalog.to_jsonb(OLD) = pg_catalog.to_jsonb(NEW) THEN
        RETURN NEW;
    END IF;

    PERFORM pg_catalog.pg_notify('sireader_observability_v1', resource_key);

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS sireader_observability_notify ON public.runtime_control_state;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.runtime_control_state
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.runtime_control_state;
CREATE TRIGGER sireader_observability_truncate
AFTER TRUNCATE ON public.runtime_control_state
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_notify ON public.ai_usage_logs;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.ai_usage_logs
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.ai_usage_logs;
CREATE TRIGGER sireader_observability_truncate
AFTER TRUNCATE ON public.ai_usage_logs
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_notify ON public.ai_analysis_logs;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.ai_analysis_logs
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.ai_analysis_logs;
CREATE TRIGGER sireader_observability_truncate
AFTER TRUNCATE ON public.ai_analysis_logs
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_notify ON public.ai_pending_messages;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.ai_pending_messages
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.ai_pending_messages;
CREATE TRIGGER sireader_observability_truncate
AFTER TRUNCATE ON public.ai_pending_messages
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_notify ON public.trade_decision_watches;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT OR UPDATE OR DELETE ON public.trade_decision_watches
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.trade_decision_watches;
CREATE TRIGGER sireader_observability_truncate
AFTER TRUNCATE ON public.trade_decision_watches
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_observability_v1();

-- Approval decisions are append-only in the observability contract.
DROP TRIGGER IF EXISTS sireader_observability_notify ON public.approval_decisions;
CREATE TRIGGER sireader_observability_notify
AFTER INSERT ON public.approval_decisions
FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_observability_v1();

DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.approval_decisions;
CREATE TRIGGER sireader_observability_truncate
AFTER TRUNCATE ON public.approval_decisions
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_observability_v1();

COMMIT;
