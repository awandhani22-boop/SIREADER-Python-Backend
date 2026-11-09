-- SIREADER Phase 5.1 — isolated human-supervised workforce commands.
-- Apply only after provisioning the dedicated sireader_workforce_cmd login role.
-- This migration is NOT an instruction to alter Supabase LIVE.
BEGIN;

DO $guard$
DECLARE
    cmd_role record;
BEGIN
    SELECT * INTO cmd_role FROM pg_catalog.pg_roles
    WHERE rolname = 'sireader_workforce_cmd';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Pre-provision dedicated login role sireader_workforce_cmd before this migration';
    END IF;
    IF NOT cmd_role.rolcanlogin OR cmd_role.rolsuper OR cmd_role.rolcreatedb
       OR cmd_role.rolcreaterole OR cmd_role.rolreplication OR cmd_role.rolbypassrls
       OR cmd_role.rolinherit THEN
        RAISE EXCEPTION 'sireader_workforce_cmd role attributes violate least privilege';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member = 'sireader_workforce_cmd'::regrole
    ) THEN
        RAISE EXCEPTION 'sireader_workforce_cmd must not inherit or SET any other role';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required Phase 4 view owner/API roles are missing';
    END IF;
END;
$guard$;

ALTER TABLE public.workforce_workers
    ADD COLUMN IF NOT EXISTS accepting_tasks boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS public.workforce_command_audit (
    command_id varchar(64) PRIMARY KEY,
    actor_id varchar(64) NOT NULL,
    command_type varchar(40) NOT NULL CHECK (
        command_type IN ('TASK_ENQUEUE','TASK_CANCEL','TASK_RETRY','WORKER_SCHEDULING_SET')
    ),
    target_id varchar(64) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    request_fingerprint varchar(64) NOT NULL CHECK (length(request_fingerprint) = 64),
    correlation_id varchar(64) NOT NULL,
    outcome jsonb NOT NULL CHECK (jsonb_typeof(outcome) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_workforce_command_actor_idempotency UNIQUE(actor_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_workforce_command_audit_created
    ON public.workforce_command_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_workforce_command_audit_correlation
    ON public.workforce_command_audit(correlation_id);

-- The audit ledger is append-only even if a future grant is accidentally widened.
CREATE OR REPLACE FUNCTION public.sireader_reject_command_audit_mutation_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
BEGIN
    RAISE EXCEPTION 'workforce command audit is append-only';
END;
$function$;

DROP TRIGGER IF EXISTS sireader_command_audit_immutable ON public.workforce_command_audit;
CREATE TRIGGER sireader_command_audit_immutable
BEFORE UPDATE OR DELETE ON public.workforce_command_audit
FOR EACH ROW EXECUTE FUNCTION public.sireader_reject_command_audit_mutation_v1();

ALTER TABLE public.workforce_command_audit ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workforce_cmd_audit_select ON public.workforce_command_audit;
CREATE POLICY workforce_cmd_audit_select
ON public.workforce_command_audit FOR SELECT TO sireader_workforce_cmd USING (true);
DROP POLICY IF EXISTS workforce_cmd_audit_insert ON public.workforce_command_audit;
CREATE POLICY workforce_cmd_audit_insert
ON public.workforce_command_audit FOR INSERT TO sireader_workforce_cmd WITH CHECK (actor_id = 'local_operator');

-- Command API can read only the workforce rows it needs, and mutate only its
-- bounded command tables. No direct access is granted to heartbeat, approval,
-- Telegram, trading, runtime-control, AI pipeline, or provider usage tables.
DO $policies$
BEGIN
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_workers';
    EXECUTE 'CREATE POLICY workforce_cmd_select ON public.workforce_workers FOR SELECT TO sireader_workforce_cmd USING (true)';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_workers';
    EXECUTE 'CREATE POLICY workforce_cmd_update ON public.workforce_workers FOR UPDATE TO sireader_workforce_cmd USING (true) WITH CHECK (true)';

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_capabilities';
    EXECUTE 'CREATE POLICY workforce_cmd_select ON public.workforce_capabilities FOR SELECT TO sireader_workforce_cmd USING (true)';

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_worker_capabilities';
    EXECUTE 'CREATE POLICY workforce_cmd_select ON public.workforce_worker_capabilities FOR SELECT TO sireader_workforce_cmd USING (true)';

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_tasks';
    EXECUTE 'CREATE POLICY workforce_cmd_select ON public.workforce_tasks FOR SELECT TO sireader_workforce_cmd USING (true)';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_insert ON public.workforce_tasks';
    EXECUTE 'CREATE POLICY workforce_cmd_insert ON public.workforce_tasks FOR INSERT TO sireader_workforce_cmd WITH CHECK (true)';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_tasks';
    EXECUTE 'CREATE POLICY workforce_cmd_update ON public.workforce_tasks FOR UPDATE TO sireader_workforce_cmd USING (true) WITH CHECK (true)';

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_task_attempts';
    EXECUTE 'CREATE POLICY workforce_cmd_select ON public.workforce_task_attempts FOR SELECT TO sireader_workforce_cmd USING (true)';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_task_attempts';
    EXECUTE 'CREATE POLICY workforce_cmd_update ON public.workforce_task_attempts FOR UPDATE TO sireader_workforce_cmd USING (true) WITH CHECK (true)';

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_insert ON public.workforce_activity_events';
    EXECUTE 'CREATE POLICY workforce_cmd_insert ON public.workforce_activity_events FOR INSERT TO sireader_workforce_cmd WITH CHECK (true)';
END;
$policies$;

GRANT USAGE ON SCHEMA public TO sireader_workforce_cmd;
GRANT SELECT ON public.workforce_workers, public.workforce_capabilities,
    public.workforce_worker_capabilities, public.workforce_tasks,
    public.workforce_task_attempts, public.workforce_command_audit
    TO sireader_workforce_cmd;
GRANT INSERT ON public.workforce_tasks, public.workforce_activity_events,
    public.workforce_command_audit TO sireader_workforce_cmd;
GRANT UPDATE (accepting_tasks, updated_at)
    ON public.workforce_workers TO sireader_workforce_cmd;
GRANT UPDATE (status, updated_at, payload_expires_at, lease_token, lease_owner, lease_expires_at)
    ON public.workforce_tasks TO sireader_workforce_cmd;
GRANT UPDATE (status, error_code, finished_at)
    ON public.workforce_task_attempts TO sireader_workforce_cmd;

REVOKE ALL ON public.workforce_command_audit FROM PUBLIC, sireader_api_ro, sireader_view_owner;
REVOKE ALL ON public.workforce_workers, public.workforce_capabilities,
    public.workforce_worker_capabilities, public.workforce_tasks,
    public.workforce_task_attempts, public.workforce_activity_events
    FROM sireader_api_ro;

GRANT SELECT (accepting_tasks) ON public.workforce_workers TO sireader_view_owner;

-- Preserve the existing read-only DTO shape and append the scheduling flag.
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
        WHERE wc.worker_id = w.worker_id AND wc.enabled AND c.enabled
    ), '[]'::jsonb) AS capability_ids,
    w.accepting_tasks
FROM public.workforce_workers AS w
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN COALESCE(bool_or(process_state = 'READY' AND heartbeat_at >= now() - interval '60 seconds'), false) THEN 'READY'
            WHEN COALESCE(bool_or(process_state = 'STARTING' AND heartbeat_at >= now() - interval '60 seconds'), false) THEN 'STARTING'
            ELSE (array_agg(process_state ORDER BY heartbeat_at DESC))[1]
        END AS process_state,
        MAX(heartbeat_at) AS heartbeat_at,
        COALESCE(SUM(active_task_count) FILTER (WHERE heartbeat_at >= now() - interval '60 seconds'), 0)::integer AS active_task_count,
        COALESCE(SUM(in_flight_provider_requests) FILTER (WHERE heartbeat_at >= now() - interval '60 seconds'), 0)::integer AS in_flight_provider_requests
    FROM public.workforce_heartbeats
    WHERE worker_id = w.worker_id
) AS h ON true;

ALTER VIEW public.v_api_workforce_workers OWNER TO sireader_view_owner;
REVOKE ALL ON public.v_api_workforce_workers FROM PUBLIC;
GRANT SELECT ON public.v_api_workforce_workers TO sireader_api_ro;

COMMIT;
