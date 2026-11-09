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
-- RLS narrows the dedicated database login to generic non-trading rows.
-- The API handler applies the same allowlists; SQL remains the hard boundary.
DO $policies$
DECLARE
    worker_allow text := $$worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist')$$;
    capability_allow text := $$capability_id IN (
        'instruction_intake','task_decomposition','workflow_supervision',
        'task_assignment_oversight','result_review','quick_research',
        'information_extraction','summarization','lightweight_classification',
        'data_analysis','comparison','evaluation','structured_reasoning',
        'complex_reasoning','strategic_analysis','deep_review','data_archiving',
        'artifact_indexing','historical_retrieval_simulation','data_organization'
    )$$;
    task_allow text := $task$
        required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
        )
        AND task_type IN (
            'INSTRUCTION_INTAKE','ROUTING','TASK_DECOMPOSITION','WORKFLOW_SUPERVISION',
            'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH','RESEARCH',
            'INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION','LIGHTWEIGHT_CLASSIFICATION',
            'DATA_ANALYSIS','REASONING','COMPARISON','EVALUATION','STRUCTURED_REASONING',
            'COMPLEX_REASONING','ESCALATION','STRATEGIC_ANALYSIS','DEEP_REVIEW',
            'DATA_ARCHIVING','ARCHIVAL','ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION',
            'DATA_ORGANIZATION'
        )
        AND (requested_worker_id IS NULL OR requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
        AND (assigned_worker_id IS NULL OR assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    $task$;
    task_reference text := format(
        'EXISTS (SELECT 1 FROM public.workforce_tasks AS t WHERE t.task_id = %I.task_id AND (%s))',
        'workforce_task_attempts', task_allow
    );
    activity_task_reference text := format(
        'EXISTS (SELECT 1 FROM public.workforce_tasks AS t WHERE t.task_id = %I.task_id AND (%s))',
        'workforce_activity_events', task_allow
    );
BEGIN
    -- Clean up the original broad Phase 5 draft policy names on reapplication.
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_workers';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_workers';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_capabilities';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_worker_capabilities';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_tasks';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_insert ON public.workforce_tasks';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_tasks';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_select ON public.workforce_task_attempts';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_update ON public.workforce_task_attempts';
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_insert ON public.workforce_activity_events';

    EXECUTE 'ALTER TABLE public.workforce_workers ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.workforce_capabilities ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.workforce_worker_capabilities ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.workforce_tasks ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.workforce_task_attempts ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.workforce_activity_events ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE public.workforce_command_audit ENABLE ROW LEVEL SECURITY';

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_workers_select ON public.workforce_workers';
    EXECUTE format('CREATE POLICY workforce_cmd_workers_select ON public.workforce_workers FOR SELECT TO sireader_workforce_cmd USING (%s)', worker_allow);
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_workers_update ON public.workforce_workers';
    EXECUTE format('CREATE POLICY workforce_cmd_workers_update ON public.workforce_workers FOR UPDATE TO sireader_workforce_cmd USING (%s) WITH CHECK (%s)', worker_allow, worker_allow);

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_capabilities_select ON public.workforce_capabilities';
    EXECUTE format('CREATE POLICY workforce_cmd_capabilities_select ON public.workforce_capabilities FOR SELECT TO sireader_workforce_cmd USING (%s)', capability_allow);

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_worker_capabilities_select ON public.workforce_worker_capabilities';
    EXECUTE format('CREATE POLICY workforce_cmd_worker_capabilities_select ON public.workforce_worker_capabilities FOR SELECT TO sireader_workforce_cmd USING ((%s) AND (%s))', worker_allow, capability_allow);

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_tasks_select ON public.workforce_tasks';
    EXECUTE format('CREATE POLICY workforce_cmd_tasks_select ON public.workforce_tasks FOR SELECT TO sireader_workforce_cmd USING (%s)', task_allow);
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_tasks_insert ON public.workforce_tasks';
    EXECUTE format('CREATE POLICY workforce_cmd_tasks_insert ON public.workforce_tasks FOR INSERT TO sireader_workforce_cmd WITH CHECK (%s)', task_allow);
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_tasks_update ON public.workforce_tasks';
    EXECUTE format('CREATE POLICY workforce_cmd_tasks_update ON public.workforce_tasks FOR UPDATE TO sireader_workforce_cmd USING (%s) WITH CHECK (%s)', task_allow, task_allow);

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_attempts_select ON public.workforce_task_attempts';
    EXECUTE format(
        'CREATE POLICY workforce_cmd_attempts_select ON public.workforce_task_attempts FOR SELECT TO sireader_workforce_cmd USING (worker_id IN (''supervisor'',''haiku'',''sonnet'',''opus'',''data_archivist'') AND %s)',
        task_reference
    );
    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_attempts_update ON public.workforce_task_attempts';
    EXECUTE format(
        'CREATE POLICY workforce_cmd_attempts_update ON public.workforce_task_attempts FOR UPDATE TO sireader_workforce_cmd USING (worker_id IN (''supervisor'',''haiku'',''sonnet'',''opus'',''data_archivist'') AND %s) WITH CHECK (worker_id IN (''supervisor'',''haiku'',''sonnet'',''opus'',''data_archivist'') AND %s)',
        task_reference, task_reference
    );

    EXECUTE 'DROP POLICY IF EXISTS workforce_cmd_activity_insert ON public.workforce_activity_events';
    EXECUTE format(
        'CREATE POLICY workforce_cmd_activity_insert ON public.workforce_activity_events FOR INSERT TO sireader_workforce_cmd WITH CHECK ((event_type = ''WORKER_STATE_CHANGED'' AND worker_id IN (''supervisor'',''haiku'',''sonnet'',''opus'',''data_archivist'') AND task_id IS NULL) OR (event_type IN (''TASK_ENQUEUED'',''TASK_CANCELLED'',''TASK_WAITING_REVIEW'') AND task_id IS NOT NULL AND (worker_id IS NULL OR worker_id IN (''supervisor'',''haiku'',''sonnet'',''opus'',''data_archivist'')) AND %s))',
        activity_task_reference
    );
END;
$policies$;

DROP POLICY IF EXISTS workforce_cmd_audit_select ON public.workforce_command_audit;
CREATE POLICY workforce_cmd_audit_select
ON public.workforce_command_audit FOR SELECT TO sireader_workforce_cmd USING (actor_id = 'local_operator');
DROP POLICY IF EXISTS workforce_cmd_audit_insert ON public.workforce_command_audit;
CREATE POLICY workforce_cmd_audit_insert
ON public.workforce_command_audit FOR INSERT TO sireader_workforce_cmd WITH CHECK (actor_id = 'local_operator');

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
