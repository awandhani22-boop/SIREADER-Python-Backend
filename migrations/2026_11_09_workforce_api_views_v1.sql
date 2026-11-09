-- Phase 4.4 read-only workforce views. Requires 2026_11_09_workforce_runtime_v1.sql.
BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.workforce_workers') IS NULL
       OR to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_activity_events') IS NULL THEN
        RAISE EXCEPTION 'Apply the Phase 4.3 workforce schema migration before API views';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required SIREADER API roles are missing';
    END IF;
END;
$guard$;

CREATE OR REPLACE VIEW public.v_api_workforce_workers
WITH (security_barrier = true) AS
SELECT
    w.worker_id,
    w.display_name,
    w.role,
    w.department,
    w.enabled,
    w.max_concurrency,
    w.model_tier,
    CASE
        WHEN NOT w.enabled THEN 'DISABLED'
        WHEN h.heartbeat_at IS NULL THEN 'OFFLINE'
        WHEN h.heartbeat_at >= now() - interval '60 seconds'
             AND h.process_state = 'READY' THEN 'READY'
        WHEN h.heartbeat_at >= now() - interval '60 seconds' THEN h.process_state
        ELSE 'STALE'
    END::varchar(20) AS process_status,
    h.heartbeat_at,
    COALESCE(h.active_task_count, 0)::integer AS active_task_count,
    COALESCE(h.in_flight_provider_requests, 0)::integer AS in_flight_provider_requests,
    w.schema_version,
    w.created_at,
    w.updated_at,
    COALESCE((
        SELECT jsonb_agg(wc.capability_id ORDER BY wc.capability_id)
        FROM public.workforce_worker_capabilities AS wc
        JOIN public.workforce_capabilities AS c
          ON c.capability_id = wc.capability_id
        WHERE wc.worker_id = w.worker_id
          AND wc.enabled
          AND c.enabled
    ), '[]'::jsonb) AS capability_ids
FROM public.workforce_workers AS w
LEFT JOIN LATERAL (
    -- A logical worker may have more than one OS process instance. Keep the
    -- process state from any fresh READY instance, while aggregating counters
    -- across every fresh instance. Stale instances must not contribute activity.
    SELECT
        CASE
            WHEN COALESCE(bool_or(
                process_state = 'READY'
                AND heartbeat_at >= now() - interval '60 seconds'
            ), false) THEN 'READY'
            WHEN COALESCE(bool_or(
                process_state = 'STARTING'
                AND heartbeat_at >= now() - interval '60 seconds'
            ), false) THEN 'STARTING'
            ELSE (array_agg(process_state ORDER BY heartbeat_at DESC))[1]
        END AS process_state,
        MAX(heartbeat_at) AS heartbeat_at,
        COALESCE(SUM(active_task_count) FILTER (
            WHERE heartbeat_at >= now() - interval '60 seconds'
        ), 0)::integer AS active_task_count,
        COALESCE(SUM(in_flight_provider_requests) FILTER (
            WHERE heartbeat_at >= now() - interval '60 seconds'
        ), 0)::integer AS in_flight_provider_requests
    FROM public.workforce_heartbeats
    WHERE worker_id = w.worker_id
) AS h ON true;

CREATE OR REPLACE VIEW public.v_api_workforce_tasks
WITH (security_barrier = true) AS
SELECT
    task_id, correlation_id, schema_version, task_type, title,
    required_capability, requested_worker_id, assigned_worker_id,
    priority, status, attempts, max_attempts, not_before, deadline_at,
    lease_expires_at, failure_code, created_at, updated_at, completed_at
FROM public.workforce_tasks;

CREATE OR REPLACE VIEW public.v_api_workforce_attempts
WITH (security_barrier = true) AS
SELECT
    attempt_id, task_id, attempt_number, worker_id, status,
    billing_currency, cost_amount, error_code, started_at, finished_at, created_at
FROM public.workforce_task_attempts;

CREATE OR REPLACE VIEW public.v_api_workforce_activity
WITH (security_barrier = true) AS
SELECT
    event_id, event_type, occurred_at, worker_id, task_id, attempt_id, correlation_id
FROM public.workforce_activity_events;

CREATE OR REPLACE VIEW public.v_api_workforce_runtime
WITH (security_barrier = true) AS
SELECT
    now() AS observed_at,
    (SELECT count(*)::integer FROM public.workforce_workers WHERE enabled) AS configured_workers,
    (
        SELECT count(*)::integer FROM public.v_api_workforce_workers
        WHERE process_status = 'READY'
    ) AS ready_workers,
    (
        SELECT count(*)::integer FROM public.v_api_workforce_workers
        WHERE process_status = 'STALE'
    ) AS stale_workers,
    (
        SELECT count(*)::integer FROM public.v_api_workforce_workers
        WHERE process_status = 'OFFLINE'
    ) AS offline_workers,
    (
        SELECT count(*)::integer FROM public.workforce_tasks
        WHERE status IN ('CLAIMED', 'RUNNING')
    ) AS active_tasks,
    (
        SELECT count(*)::integer FROM public.workforce_tasks
        WHERE status IN ('QUEUED', 'RETRY_WAIT')
    ) AS queued_tasks,
    (
        SELECT count(*)::integer FROM public.workforce_tasks
        WHERE status = 'WAITING_REVIEW'
    ) AS waiting_review_tasks,
    (
        SELECT count(*)::integer FROM public.workforce_tasks
        WHERE status = 'DEAD_LETTERED'
    ) AS dead_lettered_tasks,
    (
        SELECT count(*)::integer FROM public.workforce_tasks
        WHERE status = 'COMPLETED' AND completed_at >= now() - interval '24 hours'
    ) AS completed_last_24h;

-- PostgreSQL requires the new owner to hold CREATE on the containing schema
-- when ownership is transferred. Grant it only for this migration operation.
GRANT CREATE ON SCHEMA public TO sireader_view_owner;

ALTER VIEW public.v_api_workforce_workers OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_tasks OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_attempts OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_activity OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_runtime OWNER TO sireader_view_owner;

-- The runtime view owner must never retain schema CREATE privilege.
REVOKE CREATE ON SCHEMA public FROM sireader_view_owner;

REVOKE ALL ON public.v_api_workforce_workers,
              public.v_api_workforce_tasks,
              public.v_api_workforce_attempts,
              public.v_api_workforce_activity,
              public.v_api_workforce_runtime FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sireader_api_ro;
GRANT SELECT ON public.v_api_workforce_workers,
                public.v_api_workforce_tasks,
                public.v_api_workforce_attempts,
                public.v_api_workforce_activity,
                public.v_api_workforce_runtime TO sireader_api_ro;

COMMIT;
