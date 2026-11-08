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
    -- Compare only columns that influence the public observability snapshots.
    -- Do not serialize whole rows: some source rows contain large/private text.
    IF TG_OP = 'UPDATE' THEN
        CASE TG_TABLE_NAME
            WHEN 'runtime_control_state' THEN
                IF ROW(OLD.execution_mode, OLD.trading_mode, OLD.auto_execution_enabled,
                       OLD.auto_paused, OLD.updated_at)
                   IS NOT DISTINCT FROM
                   ROW(NEW.execution_mode, NEW.trading_mode, NEW.auto_execution_enabled,
                       NEW.auto_paused, NEW.updated_at) THEN
                    RETURN NULL;
                END IF;
            WHEN 'ai_usage_logs' THEN
                IF ROW(OLD.usage_date, OLD.provider, OLD.model, OLD.tier, OLD.input_tokens,
                       OLD.output_tokens, OLD.cache_read_tokens, OLD.cache_write_tokens,
                       OLD.thinking_tokens, OLD.billing_currency, OLD.cost_rp, OLD.reserved_rp,
                       OLD.cost_usd, OLD.reserved_usd, OLD.status, OLD.created_at, OLD.completed_at)
                   IS NOT DISTINCT FROM
                   ROW(NEW.usage_date, NEW.provider, NEW.model, NEW.tier, NEW.input_tokens,
                       NEW.output_tokens, NEW.cache_read_tokens, NEW.cache_write_tokens,
                       NEW.thinking_tokens, NEW.billing_currency, NEW.cost_rp, NEW.reserved_rp,
                       NEW.cost_usd, NEW.reserved_usd, NEW.status, NEW.created_at, NEW.completed_at) THEN
                    RETURN NULL;
                END IF;
            WHEN 'ai_analysis_logs' THEN
                IF ROW(OLD.status, OLD.created_at)
                   IS NOT DISTINCT FROM ROW(NEW.status, NEW.created_at) THEN
                    RETURN NULL;
                END IF;
            WHEN 'ai_pending_messages' THEN
                IF ROW(OLD.status, OLD.reason, OLD.attempts, OLD.claimed_at,
                       OLD.created_at, OLD.updated_at)
                   IS NOT DISTINCT FROM
                   ROW(NEW.status, NEW.reason, NEW.attempts, NEW.claimed_at,
                       NEW.created_at, NEW.updated_at) THEN
                    RETURN NULL;
                END IF;
            WHEN 'trade_decision_watches' THEN
                IF ROW(OLD.symbol, OLD.status, OLD.spread_pips, OLD.maximum_spread_pips,
                       OLD.last_bid, OLD.last_ask, OLD.check_count, OLD.last_checked_at,
                       OLD.next_check_at, OLD.created_at, OLD.updated_at)
                   IS NOT DISTINCT FROM
                   ROW(NEW.symbol, NEW.status, NEW.spread_pips, NEW.maximum_spread_pips,
                       NEW.last_bid, NEW.last_ask, NEW.check_count, NEW.last_checked_at,
                       NEW.next_check_at, NEW.created_at, NEW.updated_at) THEN
                    RETURN NULL;
                END IF;
            ELSE
                RETURN NULL;
        END CASE;
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
