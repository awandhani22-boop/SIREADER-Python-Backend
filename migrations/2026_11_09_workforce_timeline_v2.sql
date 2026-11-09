-- Phase 5.2.1: Lifecycle Audit Ledger Hardening & Safe Timeline View
-- Implements deterministic sequence ordering, actor attribution, explicit retry lineage,
-- append-only database triggers, and sanitized security-barrier timeline view.
BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_activity_events') IS NULL THEN
        RAISE EXCEPTION 'Workforce base tables are missing. Apply workforce_runtime_v1.sql first.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required SIREADER API roles (sireader_view_owner, sireader_api_ro) are missing.';
    END IF;
END;
$guard$;

-- 1. Add retry lineage to workforce_tasks
ALTER TABLE public.workforce_tasks
    ADD COLUMN IF NOT EXISTS related_task_id varchar(64) REFERENCES public.workforce_tasks(task_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_workforce_tasks_related_task
    ON public.workforce_tasks(related_task_id);

-- 2. Harden workforce_activity_events
ALTER TABLE public.workforce_activity_events
    ADD COLUMN IF NOT EXISTS event_seq bigint GENERATED ALWAYS AS IDENTITY,
    ADD COLUMN IF NOT EXISTS actor_type varchar(32) NOT NULL DEFAULT 'LEGACY_UNKNOWN',
    ADD COLUMN IF NOT EXISTS actor_id varchar(128),
    ADD COLUMN IF NOT EXISTS related_task_id varchar(64) REFERENCES public.workforce_tasks(task_id) ON DELETE SET NULL;

DO $constraint_actor_type$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_workforce_activity_actor_type'
          AND conrelid = 'public.workforce_activity_events'::regclass
    ) THEN
        ALTER TABLE public.workforce_activity_events
            ADD CONSTRAINT ck_workforce_activity_actor_type
            CHECK (actor_type IN ('SYSTEM', 'WORKER', 'OPERATOR', 'LEGACY_UNKNOWN'));
    END IF;
END;
$constraint_actor_type$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_workforce_activity_seq
    ON public.workforce_activity_events(event_seq);

CREATE INDEX IF NOT EXISTS ix_workforce_activity_task_seq
    ON public.workforce_activity_events(task_id, event_seq ASC);

CREATE INDEX IF NOT EXISTS ix_workforce_activity_related_task
    ON public.workforce_activity_events(related_task_id);

CREATE INDEX IF NOT EXISTS ix_workforce_activity_actor
    ON public.workforce_activity_events(actor_type, actor_id);

-- 3. Immutability protection triggers for workforce_activity_events
CREATE OR REPLACE FUNCTION public.sireader_reject_activity_event_mutation_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
BEGIN
    RAISE EXCEPTION 'workforce activity events are append-only';
END;
$function$;

DROP TRIGGER IF EXISTS sireader_activity_event_immutable ON public.workforce_activity_events;
CREATE TRIGGER sireader_activity_event_immutable
BEFORE UPDATE OR DELETE ON public.workforce_activity_events
FOR EACH ROW EXECUTE FUNCTION public.sireader_reject_activity_event_mutation_v1();

DROP TRIGGER IF EXISTS sireader_activity_event_truncate_immutable ON public.workforce_activity_events;
CREATE TRIGGER sireader_activity_event_truncate_immutable
BEFORE TRUNCATE ON public.workforce_activity_events
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_reject_activity_event_mutation_v1();

-- 4. Update existing views to expose non-sensitive hardened columns
CREATE OR REPLACE VIEW public.v_api_workforce_tasks
WITH (security_barrier = true) AS
SELECT
    task_id, correlation_id, schema_version, task_type, title,
    required_capability, requested_worker_id, assigned_worker_id,
    priority, status, attempts, max_attempts, not_before, deadline_at,
    lease_expires_at, failure_code, created_at, updated_at, completed_at,
    related_task_id
FROM public.workforce_tasks;

CREATE OR REPLACE VIEW public.v_api_workforce_activity
WITH (security_barrier = true) AS
SELECT
    event_id, event_type, occurred_at, worker_id, task_id, attempt_id, correlation_id,
    event_seq, actor_type, actor_id, related_task_id
FROM public.workforce_activity_events;

-- 5. Create dedicated sanitized security-barrier timeline view
CREATE OR REPLACE VIEW public.v_api_workforce_task_timeline
WITH (security_barrier = true) AS
SELECT
    e.event_seq,
    e.event_id,
    e.task_id,
    e.event_type,
    e.occurred_at,
    e.actor_type,
    e.actor_id,
    e.worker_id,
    e.attempt_id,
    a.attempt_number,
    e.related_task_id,
    e.correlation_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'error_code', e.payload->>'error_code',
            'retryable', CASE WHEN e.payload ? 'retryable' THEN (e.payload->>'retryable')::boolean ELSE NULL END,
            'outcome_unknown', CASE WHEN e.payload ? 'outcome_unknown' THEN (e.payload->>'outcome_unknown')::boolean ELSE NULL END,
            'provider_outcome_unknown', CASE WHEN e.payload ? 'provider_outcome_unknown' THEN (e.payload->>'provider_outcome_unknown')::boolean ELSE NULL END,
            'cancelled', CASE WHEN e.payload ? 'cancelled' THEN (e.payload->>'cancelled')::boolean ELSE NULL END,
            'source', e.payload->>'source',
            'task_type', e.payload->>'task_type',
            'required_capability', e.payload->>'required_capability',
            'priority', CASE WHEN e.payload ? 'priority' THEN (e.payload->>'priority')::integer ELSE NULL END,
            'attempt_number', CASE WHEN e.payload ? 'attempt_number' THEN (e.payload->>'attempt_number')::integer ELSE NULL END,
            'retry_of_task_id', e.payload->>'retry_of_task_id',
            'retried_into_task_id', e.payload->>'retried_into_task_id'
        )
    ) AS event_summary
FROM public.workforce_activity_events AS e
LEFT JOIN public.workforce_task_attempts AS a
  ON a.attempt_id = e.attempt_id;

-- 6. Grant select on new columns to view owner
GRANT SELECT (related_task_id) ON public.workforce_tasks TO sireader_view_owner;
GRANT SELECT (event_seq, actor_type, actor_id, related_task_id, payload) ON public.workforce_activity_events TO sireader_view_owner;

-- 7. View permissions and ownership
GRANT CREATE ON SCHEMA public TO sireader_view_owner;

ALTER VIEW public.v_api_workforce_tasks OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_activity OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_task_timeline OWNER TO sireader_view_owner;

REVOKE CREATE ON SCHEMA public FROM sireader_view_owner;

REVOKE ALL ON public.v_api_workforce_task_timeline FROM PUBLIC;
GRANT SELECT ON public.v_api_workforce_task_timeline TO sireader_api_ro;

COMMIT;
