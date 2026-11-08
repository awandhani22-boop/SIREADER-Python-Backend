-- Roll back SIREADER Phase 3.3 observability notifications.
-- Run through the approved database-owner migration process.

BEGIN;

DROP TRIGGER IF EXISTS sireader_observability_notify ON public.runtime_control_state;
DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.runtime_control_state;
DROP TRIGGER IF EXISTS sireader_observability_notify ON public.ai_usage_logs;
DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.ai_usage_logs;
DROP TRIGGER IF EXISTS sireader_observability_notify ON public.ai_analysis_logs;
DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.ai_analysis_logs;
DROP TRIGGER IF EXISTS sireader_observability_notify ON public.ai_pending_messages;
DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.ai_pending_messages;
DROP TRIGGER IF EXISTS sireader_observability_notify ON public.trade_decision_watches;
DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.trade_decision_watches;
DROP TRIGGER IF EXISTS sireader_observability_notify ON public.approval_decisions;
DROP TRIGGER IF EXISTS sireader_observability_truncate ON public.approval_decisions;

DROP FUNCTION IF EXISTS public.sireader_notify_observability_v1();

COMMIT;
