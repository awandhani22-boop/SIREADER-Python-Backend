-- SIREADER production migration: read-only workforce observability views.
-- Requires workforce_runtime_schema_v1 and the existing Phase 3.1 view-owner/API roles.
-- All views are security-barrier views owned by the NOLOGIN sireader_view_owner role.

BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.workforce_workers') IS NULL
       OR to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_activity_events') IS NULL
       OR to_regclass('public.ai_usage_logs') IS NULL THEN
        RAISE EXCEPTION 'Required workforce or Cost Guard base tables are missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required Phase 3.1 SIREADER roles are missing';
    END IF;
    IF has_schema_privilege('sireader_view_owner', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'sireader_view_owner unexpectedly has CREATE on public before the migration';
    END IF;
END;
$guard$;

CREATE VIEW public.v_api_workforce_workers
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
    ), '[]'::jsonb) AS capability_ids,
    w.accepting_tasks
FROM public.workforce_workers AS w
LEFT JOIN LATERAL (
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

CREATE VIEW public.v_api_workforce_tasks
WITH (security_barrier = true) AS
SELECT
    task_id, correlation_id, schema_version, task_type, title,
    required_capability, requested_worker_id, assigned_worker_id,
    priority, status, attempts, max_attempts, not_before, deadline_at,
    lease_expires_at, failure_code, created_at, updated_at, completed_at,
    related_task_id
FROM public.workforce_tasks;

CREATE VIEW public.v_api_workforce_attempts
WITH (security_barrier = true) AS
SELECT
    attempt_id, task_id, attempt_number, worker_id, status,
    billing_currency, cost_amount, error_code, started_at, finished_at, created_at
FROM public.workforce_task_attempts;

CREATE VIEW public.v_api_workforce_activity
WITH (security_barrier = true) AS
SELECT
    event_id, event_type, occurred_at, worker_id, task_id, attempt_id,
    correlation_id, event_seq, actor_type, actor_id, related_task_id
FROM public.workforce_activity_events;

CREATE VIEW public.v_api_workforce_runtime
WITH (security_barrier = true) AS
SELECT
    now() AS observed_at,
    (SELECT count(*)::integer
        FROM public.workforce_workers WHERE enabled) AS configured_workers,
    (SELECT count(*)::integer
        FROM public.v_api_workforce_workers
        WHERE process_status = 'READY') AS ready_workers,
    (SELECT count(*)::integer
        FROM public.v_api_workforce_workers
        WHERE process_status = 'STALE') AS stale_workers,
    (SELECT count(*)::integer
        FROM public.v_api_workforce_workers
        WHERE process_status = 'OFFLINE') AS offline_workers,
    (SELECT count(*)::integer
        FROM public.workforce_tasks
        WHERE status IN ('CLAIMED','RUNNING')) AS active_tasks,
    (SELECT count(*)::integer
        FROM public.workforce_tasks
        WHERE status IN ('QUEUED','RETRY_WAIT')) AS queued_tasks,
    (SELECT count(*)::integer
        FROM public.workforce_tasks
        WHERE status = 'WAITING_REVIEW') AS waiting_review_tasks,
    (SELECT count(*)::integer
        FROM public.workforce_tasks
        WHERE status = 'DEAD_LETTERED') AS dead_lettered_tasks,
    (SELECT count(*)::integer
        FROM public.workforce_tasks
        WHERE status = 'COMPLETED'
          AND completed_at >= now() - interval '24 hours') AS completed_last_24h;

CREATE VIEW public.v_api_workforce_task_timeline
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
            'retryable', CASE WHEN e.payload ? 'retryable'
                THEN (e.payload->>'retryable')::boolean ELSE NULL END,
            'outcome_unknown', CASE WHEN e.payload ? 'outcome_unknown'
                THEN (e.payload->>'outcome_unknown')::boolean ELSE NULL END,
            'provider_outcome_unknown', CASE WHEN e.payload ? 'provider_outcome_unknown'
                THEN (e.payload->>'provider_outcome_unknown')::boolean ELSE NULL END,
            'cancelled', CASE WHEN e.payload ? 'cancelled'
                THEN (e.payload->>'cancelled')::boolean ELSE NULL END,
            'source', e.payload->>'source',
            'task_type', e.payload->>'task_type',
            'required_capability', e.payload->>'required_capability',
            'priority', CASE WHEN e.payload ? 'priority'
                THEN (e.payload->>'priority')::integer ELSE NULL END,
            'attempt_number', CASE WHEN e.payload ? 'attempt_number'
                THEN (e.payload->>'attempt_number')::integer ELSE NULL END,
            'retry_of_task_id', e.payload->>'retry_of_task_id',
            'retried_into_task_id', e.payload->>'retried_into_task_id'
        )
    ) AS event_summary
FROM public.workforce_activity_events AS e
LEFT JOIN public.workforce_task_attempts AS a
  ON a.attempt_id = e.attempt_id;

CREATE VIEW public.v_api_workforce_ai_usage
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

-- Additional minimum column grants required by the timeline and cost views.
GRANT SELECT (request_id) ON public.ai_usage_logs TO sireader_view_owner;

-- Change ownership only during this migration. The view owner must not retain CREATE.
GRANT CREATE ON SCHEMA public TO sireader_view_owner;

ALTER VIEW public.v_api_workforce_workers OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_tasks OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_attempts OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_activity OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_runtime OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_task_timeline OWNER TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_ai_usage OWNER TO sireader_view_owner;

REVOKE CREATE ON SCHEMA public FROM sireader_view_owner;

REVOKE ALL ON
    public.v_api_workforce_workers,
    public.v_api_workforce_tasks,
    public.v_api_workforce_attempts,
    public.v_api_workforce_activity,
    public.v_api_workforce_runtime,
    public.v_api_workforce_task_timeline,
    public.v_api_workforce_ai_usage
FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;

GRANT SELECT ON
    public.v_api_workforce_workers,
    public.v_api_workforce_tasks,
    public.v_api_workforce_attempts,
    public.v_api_workforce_activity,
    public.v_api_workforce_runtime,
    public.v_api_workforce_task_timeline,
    public.v_api_workforce_ai_usage
TO sireader_api_ro;

COMMIT;
