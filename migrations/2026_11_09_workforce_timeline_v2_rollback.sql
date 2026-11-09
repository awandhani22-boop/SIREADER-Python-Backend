-- Rollback migration for Phase 5.2.1 timeline hardening
BEGIN;

-- 1. Drop timeline view and views being restored to v1 schema
DROP VIEW IF EXISTS public.v_api_workforce_task_timeline CASCADE;
DROP VIEW IF EXISTS public.v_api_workforce_tasks CASCADE;
DROP VIEW IF EXISTS public.v_api_workforce_activity CASCADE;

-- 2. Restore v1 views
CREATE VIEW public.v_api_workforce_tasks
WITH (security_barrier = true) AS
SELECT
    task_id, correlation_id, schema_version, task_type, title,
    required_capability, requested_worker_id, assigned_worker_id,
    priority, status, attempts, max_attempts, not_before, deadline_at,
    lease_expires_at, failure_code, created_at, updated_at, completed_at
FROM public.workforce_tasks;

CREATE VIEW public.v_api_workforce_activity
WITH (security_barrier = true) AS
SELECT
    event_id, event_type, occurred_at, worker_id, task_id, attempt_id, correlation_id
FROM public.workforce_activity_events;

GRANT CREATE ON SCHEMA public TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_tasks OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_activity OWNER TO sireader_view_owner;
REVOKE CREATE ON SCHEMA public FROM sireader_view_owner;

GRANT SELECT ON public.v_api_workforce_tasks TO sireader_api_ro;
GRANT SELECT ON public.v_api_workforce_activity TO sireader_api_ro;

-- 3. Drop triggers
DROP TRIGGER IF EXISTS sireader_activity_event_immutable ON public.workforce_activity_events;
DROP TRIGGER IF EXISTS sireader_activity_event_truncate_immutable ON public.workforce_activity_events;
DROP FUNCTION IF EXISTS public.sireader_reject_activity_event_mutation_v1();

-- 4. Drop added columns and constraints
ALTER TABLE public.workforce_activity_events
    DROP CONSTRAINT IF EXISTS ck_workforce_activity_actor_type;

DROP INDEX IF EXISTS public.uq_workforce_activity_seq;
DROP INDEX IF EXISTS public.ix_workforce_activity_task_seq;
DROP INDEX IF EXISTS public.ix_workforce_activity_related_task;
DROP INDEX IF EXISTS public.ix_workforce_activity_actor;

ALTER TABLE public.workforce_activity_events
    DROP COLUMN IF EXISTS related_task_id,
    DROP COLUMN IF EXISTS actor_id,
    DROP COLUMN IF EXISTS actor_type,
    DROP COLUMN IF EXISTS event_seq;

DROP INDEX IF EXISTS public.uq_workforce_tasks_related_task;
DROP INDEX IF EXISTS public.ix_workforce_tasks_related_task;

ALTER TABLE public.workforce_tasks
    DROP COLUMN IF EXISTS related_task_id;

COMMIT;
