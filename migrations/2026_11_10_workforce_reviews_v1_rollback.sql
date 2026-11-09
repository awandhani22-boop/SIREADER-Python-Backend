-- Rollback for SIREADER Phase 5.2.2 migration 2026_11_10_workforce_reviews_v1.sql
BEGIN;

REVOKE SELECT ON public.v_api_workforce_task_reviews FROM sireader_api_ro;
REVOKE SELECT, INSERT, UPDATE ON public.workforce_task_reviews FROM sireader_workforce_cmd;
REVOKE SELECT, INSERT, UPDATE ON public.workforce_task_review_payloads FROM sireader_workforce_cmd;

DROP VIEW IF EXISTS public.v_api_workforce_task_reviews;

DROP TRIGGER IF EXISTS trg_workforce_task_review_payloads_guard ON public.workforce_task_review_payloads;
DROP FUNCTION IF EXISTS public.sireader_guard_review_payloads_v1();

DROP TRIGGER IF EXISTS trg_workforce_task_reviews_guard ON public.workforce_task_reviews;
DROP FUNCTION IF EXISTS public.sireader_guard_review_metadata_v1();

DROP TABLE IF EXISTS public.workforce_task_review_payloads;
DROP TABLE IF EXISTS public.workforce_task_reviews;

-- Revert command audit constraint to original 4 command types
ALTER TABLE public.workforce_command_audit
    DROP CONSTRAINT IF EXISTS workforce_command_audit_command_type_check;

ALTER TABLE public.workforce_command_audit
    ADD CONSTRAINT workforce_command_audit_command_type_check CHECK (
        command_type::text = ANY (ARRAY['TASK_ENQUEUE'::character varying, 'TASK_CANCEL'::character varying, 'TASK_RETRY'::character varying, 'WORKER_SCHEDULING_SET'::character varying]::text[])
    );

-- Revert activity events constraint to original 12 event types
ALTER TABLE public.workforce_activity_events
    DROP CONSTRAINT IF EXISTS workforce_activity_events_event_type_check;

ALTER TABLE public.workforce_activity_events
    ADD CONSTRAINT workforce_activity_events_event_type_check CHECK (
        event_type::text = ANY (ARRAY[
            'TASK_ENQUEUED'::character varying, 'TASK_CLAIMED'::character varying,
            'TASK_STARTED'::character varying, 'PROVIDER_REQUESTED'::character varying,
            'TASK_RETRIED'::character varying, 'TASK_COMPLETED'::character varying,
            'TASK_FAILED'::character varying, 'TASK_CANCELLED'::character varying,
            'TASK_WAITING_REVIEW'::character varying, 'TASK_DEAD_LETTERED'::character varying,
            'WORKER_HEARTBEAT'::character varying, 'WORKER_STATE_CHANGED'::character varying
        ]::text[])
    );

COMMIT;
