-- SIREADER Phase 7.4 follow-up: operator, trusted review, and bounded maintenance authority.
-- STAGING REHEARSAL ONLY. Never apply to Supabase LIVE. Target is a local restored PG17 clone.
BEGIN;

DO $guard$
BEGIN
    IF current_database() <> 'sireader_stage'
       OR inet_server_addr() IS DISTINCT FROM inet '127.0.0.1'
       OR inet_server_port() <> 55439
       OR session_user <> 'postgres' THEN
        RAISE EXCEPTION 'Operator/maintenance authority rehearsal is restricted to sireader_stage on 127.0.0.1:55439 as postgres';
    END IF;
    IF to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_task_reviews') IS NULL
       OR to_regclass('public.workforce_task_review_payloads') IS NULL
       OR to_regclass('sireader_workforce_private.worker_principals') IS NULL THEN
        RAISE EXCEPTION 'The previously accepted worker command layer and review schema are required';
    END IF;
END;
$guard$;

DO $roles$
DECLARE
    v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'sireader_workforce_cmd',
        'sireader_workforce_review',
        'sireader_workforce_maint'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = v_role) THEN
            EXECUTE pg_catalog.format(
                'CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                v_role
            );
        ELSE
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = v_role
                  AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolinherit)
            ) THEN
                RAISE EXCEPTION 'Authority role % violates the staging least-privilege contract', v_role;
            END IF;
        END IF;
    END LOOP;
END;
$roles$;

GRANT USAGE ON SCHEMA sireader_workforce_private TO
    sireader_workforce_cmd, sireader_workforce_review, sireader_workforce_maint;

CREATE TABLE IF NOT EXISTS sireader_workforce_private.authority_principals (
    database_role name PRIMARY KEY,
    authority_type varchar(20) NOT NULL CHECK (authority_type IN ('COMMAND','REVIEW','MAINTENANCE')),
    actor_id varchar(64) NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
ALTER TABLE sireader_workforce_private.authority_principals OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.authority_principals FROM PUBLIC;
INSERT INTO sireader_workforce_private.authority_principals(database_role,authority_type,actor_id)
VALUES
    ('sireader_workforce_cmd','COMMAND','local_operator'),
    ('sireader_workforce_review','REVIEW','local_operator'),
    ('sireader_workforce_maint','MAINTENANCE','system')
ON CONFLICT (database_role) DO UPDATE
SET authority_type=EXCLUDED.authority_type, actor_id=EXCLUDED.actor_id, enabled=true;

CREATE TABLE IF NOT EXISTS sireader_workforce_private.authority_command_audit (
    command_id varchar(64) PRIMARY KEY,
    actor_id varchar(64) NOT NULL,
    command_type varchar(40) NOT NULL CHECK (
        command_type IN ('TASK_ENQUEUE','TASK_CANCEL','TASK_RETRY','WORKER_SCHEDULING_SET','REVIEW_ADJUDICATE')
    ),
    target_id varchar(64) NOT NULL,
    idempotency_key varchar(128) NOT NULL,
    request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    correlation_id varchar(64) NOT NULL,
    outcome jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE(actor_id,idempotency_key)
);
ALTER TABLE sireader_workforce_private.authority_command_audit OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.authority_command_audit FROM PUBLIC;

CREATE TABLE IF NOT EXISTS sireader_workforce_private.authority_maintenance_audit (
    run_id varchar(64) PRIMARY KEY,
    database_role name NOT NULL,
    actor_id varchar(64) NOT NULL,
    batch_size integer NOT NULL CHECK (batch_size BETWEEN 1 AND 500),
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    result jsonb NOT NULL
);
ALTER TABLE sireader_workforce_private.authority_maintenance_audit OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.authority_maintenance_audit FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.require_authority_v1(p_expected_type text)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text;
    v_type text;
    v_principal name := pg_catalog.split_part(session_user::text,'.',1)::name;
BEGIN
    SELECT p.actor_id,p.authority_type INTO v_actor,v_type
    FROM sireader_workforce_private.authority_principals p
    WHERE p.database_role=v_principal AND p.enabled;
    IF NOT FOUND OR v_type IS DISTINCT FROM p_expected_type THEN
        RAISE EXCEPTION 'Database identity is not authorized for this workforce operation';
    END IF;
    RETURN v_actor;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.require_authority_v1(text) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.require_authority_v1(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.command_fingerprint_v1(p_request jsonb)
RETURNS char(64)
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $function$
    SELECT pg_catalog.encode(
        extensions.digest(
            pg_catalog.convert_to(sireader_workforce_private.canonical_json_v1(p_request),'UTF8'),
            'sha256'
        ),
        'hex'
    )::char(64);
$function$;
ALTER FUNCTION sireader_workforce_private.command_fingerprint_v1(jsonb) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_fingerprint_v1(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.command_replay_v1(
    p_actor_id text,p_idempotency_key text,p_request_fingerprint char(64)
)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing sireader_workforce_private.authority_command_audit%ROWTYPE;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_actor_id || ':' || p_idempotency_key,0)
    );
    SELECT * INTO v_existing
    FROM sireader_workforce_private.authority_command_audit a
    WHERE a.actor_id=p_actor_id AND a.idempotency_key=p_idempotency_key
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_existing.request_fingerprint IS DISTINCT FROM p_request_fingerprint THEN
        RAISE EXCEPTION 'IDEMPOTENCY_CONFLICT';
    END IF;
    RETURN v_existing.outcome || pg_catalog.jsonb_build_object('replayed',true);
END;
$function$;
ALTER FUNCTION sireader_workforce_private.command_replay_v1(text,text,char) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_replay_v1(text,text,char) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.command_record_v1(
    p_actor_id text,p_command_type text,p_target_id text,p_idempotency_key text,
    p_request_fingerprint char(64),p_correlation_id text,p_outcome jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    v_command_id text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
    v_created_at timestamptz := pg_catalog.clock_timestamp();
    v_record jsonb;
BEGIN
    v_record := pg_catalog.jsonb_build_object(
        'command_id',v_command_id,
        'actor_id',p_actor_id,
        'command_type',p_command_type,
        'target_id',p_target_id,
        'correlation_id',p_correlation_id,
        'outcome',COALESCE(p_outcome,'{}'::jsonb),
        'created_at',v_created_at,
        'replayed',false
    );
    INSERT INTO sireader_workforce_private.authority_command_audit(
        command_id,actor_id,command_type,target_id,idempotency_key,
        request_fingerprint,correlation_id,outcome,created_at
    ) VALUES (
        v_command_id,p_actor_id,p_command_type,p_target_id,p_idempotency_key,
        p_request_fingerprint,p_correlation_id,v_record,v_created_at
    );
    RETURN v_record;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.command_record_v1(text,text,text,text,char,text,jsonb) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_record_v1(text,text,text,text,char,text,jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.append_operator_event_v1(
    p_event_type text,p_actor_id text,p_task_id text,p_worker_id text,
    p_correlation_id text,p_payload jsonb,p_attempt_id text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_event_type NOT IN (
        'TASK_ENQUEUED','TASK_CANCELLED','TASK_RETRIED','WORKER_STATE_CHANGED',
        'HUMAN_REVIEW_APPROVED','HUMAN_REVIEW_EDITED','HUMAN_REVIEW_REJECTED','HUMAN_REVIEW_EXPIRED',
        'TASK_WAITING_REVIEW','TASK_DEAD_LETTERED'
    ) THEN
        RAISE EXCEPTION 'Operator/maintenance event type is not allowed';
    END IF;
    INSERT INTO public.workforce_activity_events(
        event_id,event_type,occurred_at,actor_type,actor_id,worker_id,task_id,
        attempt_id,correlation_id,payload
    ) VALUES (
        pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-',''),
        p_event_type,pg_catalog.clock_timestamp(),
        CASE WHEN p_actor_id='system' THEN 'SYSTEM' ELSE 'OPERATOR' END,
        p_actor_id,p_worker_id,p_task_id,p_attempt_id,p_correlation_id,
        COALESCE(p_payload,'{}'::jsonb)
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.append_operator_event_v1(text,text,text,text,text,jsonb,text) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.append_operator_event_v1(text,text,text,text,text,jsonb,text) FROM PUBLIC;

-- Database command API: capability-aware, non-trading enqueue.
CREATE OR REPLACE FUNCTION sireader_workforce_private.command_enqueue_task_v1(
    p_task_type text,p_title text,p_required_capability text,p_requested_worker_id text,
    p_input_payload jsonb,p_priority integer,p_max_attempts integer,p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('COMMAND');
    v_request jsonb;
    v_fingerprint char(64);
    v_replay jsonb;
    v_task_id text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
    v_correlation_id text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
    v_task_idempotency text;
    v_task public.workforce_tasks%ROWTYPE;
BEGIN
    IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._~-]{8,128}$'
       OR p_task_type IS NULL OR pg_catalog.length(p_task_type) NOT BETWEEN 2 AND 64
       OR p_title IS NULL OR pg_catalog.length(pg_catalog.btrim(p_title)) NOT BETWEEN 1 AND 180
       OR p_required_capability IS NULL OR p_requested_worker_id IS NULL
       OR p_priority IS NULL OR p_priority NOT BETWEEN 1 AND 100
       OR p_max_attempts IS NULL OR p_max_attempts NOT BETWEEN 1 AND 3
       OR p_input_payload IS NULL OR pg_catalog.jsonb_typeof(p_input_payload) <> 'object'
       OR pg_catalog.octet_length(sireader_workforce_private.canonical_json_v1(p_input_payload)) > 16384 THEN
        RAISE EXCEPTION 'Command input is invalid or outside permitted bounds';
    END IF;
    IF p_required_capability NOT IN (
        'instruction_intake','task_decomposition','workflow_supervision','task_assignment_oversight',
        'result_review','quick_research','information_extraction','summarization',
        'lightweight_classification','data_analysis','comparison','evaluation',
        'structured_reasoning','complex_reasoning','strategic_analysis','deep_review',
        'data_archiving','artifact_indexing','historical_retrieval_simulation','data_organization'
    ) THEN
        RAISE EXCEPTION 'Trading-related or unknown capability is not eligible for operator commands';
    END IF;
    IF p_requested_worker_id NOT IN ('supervisor','haiku','sonnet','opus','data_archivist') THEN
        RAISE EXCEPTION 'Requested worker is not eligible for non-trading commands';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_workers w
        JOIN public.workforce_worker_capabilities wc ON wc.worker_id=w.worker_id AND wc.enabled
        JOIN public.workforce_capabilities c ON c.capability_id=wc.capability_id AND c.enabled
        WHERE w.worker_id=p_requested_worker_id AND w.enabled
          AND c.capability_id=p_required_capability AND c.task_types ? p_task_type
    ) THEN
        RAISE EXCEPTION 'Requested worker does not have the requested enabled capability/task pair';
    END IF;
    v_request := pg_catalog.jsonb_build_object(
        'command_type','TASK_ENQUEUE',
        'task_type',p_task_type,'title',pg_catalog.btrim(p_title),
        'required_capability',p_required_capability,'requested_worker_id',p_requested_worker_id,
        'input_payload',p_input_payload,'priority',p_priority,'max_attempts',p_max_attempts
    );
    v_fingerprint := sireader_workforce_private.command_fingerprint_v1(v_request);
    v_replay := sireader_workforce_private.command_replay_v1(v_actor,p_idempotency_key,v_fingerprint);
    IF v_replay IS NOT NULL THEN RETURN v_replay; END IF;

    v_task_idempotency := 'cmd5-' || pg_catalog.encode(
        extensions.digest(pg_catalog.convert_to(v_actor || ':' || p_idempotency_key,'UTF8'),'sha256'),'hex'
    );
    INSERT INTO public.workforce_tasks(
        task_id,idempotency_key,correlation_id,schema_version,task_type,title,
        required_capability,requested_worker_id,priority,status,input_payload,
        attempts,max_attempts,not_before,created_at,updated_at
    ) VALUES (
        v_task_id,v_task_idempotency,v_correlation_id,1,p_task_type,pg_catalog.btrim(p_title),
        p_required_capability,p_requested_worker_id,p_priority,'QUEUED',p_input_payload,
        0,p_max_attempts,pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp()
    ) RETURNING * INTO v_task;

    PERFORM sireader_workforce_private.append_operator_event_v1(
        'TASK_ENQUEUED',v_actor,v_task.task_id,NULL,v_correlation_id,
        pg_catalog.jsonb_build_object(
            'task_type',p_task_type,'required_capability',p_required_capability,'priority',p_priority
        )
    );
    RETURN sireader_workforce_private.command_record_v1(
        v_actor,'TASK_ENQUEUE',v_task.task_id,p_idempotency_key,v_fingerprint,
        v_correlation_id,pg_catalog.jsonb_build_object(
            'accepted',true,'created',true,'status',v_task.status,'task_id',v_task.task_id
        )
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text) FROM PUBLIC;

-- Database command API: cancellation quarantines every uncertain provider outcome.
CREATE OR REPLACE FUNCTION sireader_workforce_private.command_cancel_task_v1(
    p_task_id text,p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('COMMAND');
    v_request jsonb := pg_catalog.jsonb_build_object('command_type','TASK_CANCEL','task_id',p_task_id);
    v_fingerprint char(64);
    v_replay jsonb;
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_has_attempt boolean := false;
    v_unknown boolean := false;
    v_correlation_id text;
BEGIN
    IF p_task_id IS NULL OR p_task_id !~ '^[A-Za-z0-9_-]{8,64}$'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._~-]{8,128}$' THEN
        RAISE EXCEPTION 'Command input is invalid';
    END IF;
    v_fingerprint := sireader_workforce_private.command_fingerprint_v1(v_request);
    v_replay := sireader_workforce_private.command_replay_v1(v_actor,p_idempotency_key,v_fingerprint);
    IF v_replay IS NOT NULL THEN RETURN v_replay; END IF;

    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=p_task_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Task not found'; END IF;
    IF v_task.required_capability NOT IN (
        'instruction_intake','task_decomposition','workflow_supervision','task_assignment_oversight',
        'result_review','quick_research','information_extraction','summarization',
        'lightweight_classification','data_analysis','comparison','evaluation',
        'structured_reasoning','complex_reasoning','strategic_analysis','deep_review',
        'data_archiving','artifact_indexing','historical_retrieval_simulation','data_organization'
    ) THEN
        RAISE EXCEPTION 'Trading-related task is not eligible for operator commands';
    END IF;
    IF v_task.status IN ('COMPLETED','FAILED','CANCELLED','DEAD_LETTERED') THEN
        RETURN sireader_workforce_private.command_record_v1(
            v_actor,'TASK_CANCEL',p_task_id,p_idempotency_key,v_fingerprint,
            v_task.correlation_id,pg_catalog.jsonb_build_object(
                'accepted',true,'status',v_task.status,'provider_outcome_unknown',false,'cancelled',false
            )
        );
    END IF;

    IF v_task.lease_token IS NOT NULL THEN
        SELECT * INTO v_attempt FROM public.workforce_task_attempts
        WHERE lease_token=v_task.lease_token AND task_id=v_task.task_id FOR UPDATE;
        v_has_attempt := FOUND;
    END IF;
    IF NOT v_has_attempt THEN
        SELECT * INTO v_attempt FROM public.workforce_task_attempts
        WHERE task_id=v_task.task_id ORDER BY attempt_number DESC LIMIT 1 FOR UPDATE;
        v_has_attempt := FOUND;
    END IF;
    v_unknown := v_has_attempt AND (
        v_attempt.provider_request_id IS NOT NULL OR v_attempt.provider_started_at IS NOT NULL
        OR v_attempt.status IN ('PROVIDER_REQUESTED','UNKNOWN')
    );
    IF v_unknown THEN
        v_task.status := 'WAITING_REVIEW';
        v_attempt.status := 'UNKNOWN';
        v_attempt.error_code := 'CANCEL_AFTER_PROVIDER_REQUEST';
        v_attempt.finished_at := pg_catalog.clock_timestamp();
        UPDATE public.workforce_task_attempts SET status=v_attempt.status,error_code=v_attempt.error_code,
            finished_at=v_attempt.finished_at WHERE attempt_id=v_attempt.attempt_id;
    ELSE
        v_task.status := 'CANCELLED';
        IF v_has_attempt THEN
            UPDATE public.workforce_task_attempts SET status='CANCELLED',
                finished_at=pg_catalog.clock_timestamp() WHERE attempt_id=v_attempt.attempt_id;
        END IF;
    END IF;
    v_task.updated_at := pg_catalog.clock_timestamp();
    v_task.payload_expires_at := v_task.updated_at + interval '30 days';
    v_task.lease_token := NULL;
    v_task.lease_owner := NULL;
    v_task.lease_expires_at := NULL;
    UPDATE public.workforce_tasks SET status=v_task.status,updated_at=v_task.updated_at,
        payload_expires_at=v_task.payload_expires_at,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
    WHERE task_id=v_task.task_id;
    v_correlation_id := v_task.correlation_id;
    PERFORM sireader_workforce_private.append_operator_event_v1(
        CASE WHEN v_unknown THEN 'TASK_WAITING_REVIEW' ELSE 'TASK_CANCELLED' END,
        v_actor,v_task.task_id,v_task.assigned_worker_id,v_correlation_id,
        pg_catalog.jsonb_build_object('cancelled',true,'provider_outcome_unknown',v_unknown),
        CASE WHEN v_has_attempt THEN v_attempt.attempt_id ELSE NULL END
    );
    RETURN sireader_workforce_private.command_record_v1(
        v_actor,'TASK_CANCEL',p_task_id,p_idempotency_key,v_fingerprint,
        v_correlation_id,pg_catalog.jsonb_build_object(
            'accepted',true,'status',v_task.status,'provider_outcome_unknown',v_unknown,
            'cancelled',NOT v_unknown
        )
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.command_cancel_task_v1(text,text)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_cancel_task_v1(text,text) FROM PUBLIC;

-- Database command API: retry only when the previous provider outcome is proven not sent.
CREATE OR REPLACE FUNCTION sireader_workforce_private.command_retry_task_v1(
    p_task_id text,p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('COMMAND');
    v_request jsonb := pg_catalog.jsonb_build_object('command_type','TASK_RETRY','task_id',p_task_id);
    v_fingerprint char(64);
    v_replay jsonb;
    v_original public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_new_id text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
    v_new_correlation text;
    v_task_idempotency text;
    v_new_task public.workforce_tasks%ROWTYPE;
BEGIN
    IF p_task_id IS NULL OR p_task_id !~ '^[A-Za-z0-9_-]{8,64}$'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._~-]{8,128}$' THEN
        RAISE EXCEPTION 'Command input is invalid';
    END IF;
    v_fingerprint := sireader_workforce_private.command_fingerprint_v1(v_request);
    v_replay := sireader_workforce_private.command_replay_v1(v_actor,p_idempotency_key,v_fingerprint);
    IF v_replay IS NOT NULL THEN RETURN v_replay; END IF;
    SELECT * INTO v_original FROM public.workforce_tasks WHERE task_id=p_task_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Task not found'; END IF;
    IF v_original.required_capability NOT IN (
        'instruction_intake','task_decomposition','workflow_supervision','task_assignment_oversight',
        'result_review','quick_research','information_extraction','summarization',
        'lightweight_classification','data_analysis','comparison','evaluation',
        'structured_reasoning','complex_reasoning','strategic_analysis','deep_review',
        'data_archiving','artifact_indexing','historical_retrieval_simulation','data_organization'
    ) THEN
        RAISE EXCEPTION 'Trading-related task is not eligible for operator commands';
    END IF;
    IF v_original.status <> 'FAILED' OR v_original.payload_purged_at IS NOT NULL
       OR (v_original.requested_worker_id IS NOT NULL
           AND v_original.requested_worker_id NOT IN ('supervisor','haiku','sonnet','opus','data_archivist')) THEN
        RAISE EXCEPTION 'TASK_NOT_SAFE_TO_RETRY';
    END IF;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts
    WHERE task_id=v_original.task_id ORDER BY attempt_number DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND OR v_attempt.status <> 'FAILED' OR v_attempt.provider_request_id IS NOT NULL
       OR v_attempt.provider_started_at IS NOT NULL OR v_attempt.attempt_number <> v_original.attempts THEN
        RAISE EXCEPTION 'TASK_PROVIDER_OUTCOME_NOT_CONFIRMED_SAFE';
    END IF;
    IF EXISTS (SELECT 1 FROM public.workforce_tasks t WHERE t.related_task_id=v_original.task_id) THEN
        RAISE EXCEPTION 'TASK_ALREADY_RETRIED';
    END IF;
    v_task_idempotency := 'cmd5-' || pg_catalog.encode(
        extensions.digest(pg_catalog.convert_to(v_actor || ':' || p_idempotency_key,'UTF8'),'sha256'),'hex'
    );
    v_new_correlation := v_original.correlation_id;
    INSERT INTO public.workforce_tasks(
        task_id,idempotency_key,correlation_id,schema_version,task_type,title,
        required_capability,requested_worker_id,priority,status,input_payload,attempts,
        max_attempts,not_before,created_at,updated_at,related_task_id
    ) VALUES (
        v_new_id,v_task_idempotency,v_new_correlation,1,v_original.task_type,
        pg_catalog.left('Retry: ' || v_original.title,180),
        v_original.required_capability,v_original.requested_worker_id,v_original.priority,
        'QUEUED',v_original.input_payload,0,v_original.max_attempts,
        pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp(),pg_catalog.clock_timestamp(),
        v_original.task_id
    ) RETURNING * INTO v_new_task;
    PERFORM sireader_workforce_private.append_operator_event_v1(
        'TASK_ENQUEUED',v_actor,v_new_task.task_id,NULL,v_new_correlation,
        pg_catalog.jsonb_build_object('task_type',v_new_task.task_type,
            'required_capability',v_new_task.required_capability,'priority',v_new_task.priority,
            'retry_of_task_id',v_original.task_id)
    );
    PERFORM sireader_workforce_private.append_operator_event_v1(
        'TASK_RETRIED',v_actor,v_original.task_id,v_original.assigned_worker_id,v_new_correlation,
        pg_catalog.jsonb_build_object('retried_into_task_id',v_new_task.task_id,
            'retry_of_task_id',v_original.task_id),v_attempt.attempt_id
    );
    RETURN sireader_workforce_private.command_record_v1(
        v_actor,'TASK_RETRY',v_new_task.task_id,p_idempotency_key,v_fingerprint,
        v_new_correlation,pg_catalog.jsonb_build_object(
            'accepted',true,'created',true,'status',v_new_task.status,
            'task_id',v_new_task.task_id,'retry_of_task_id',v_original.task_id
        )
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.command_retry_task_v1(text,text)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_retry_task_v1(text,text) FROM PUBLIC;

-- Database command API: only pause/resume scheduling; never changes worker definitions.
CREATE OR REPLACE FUNCTION sireader_workforce_private.command_set_worker_scheduling_v1(
    p_worker_id text,p_accepting_tasks boolean,p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('COMMAND');
    v_request jsonb := pg_catalog.jsonb_build_object(
        'command_type','WORKER_SCHEDULING_SET',
        'worker_id',p_worker_id,'accepting_tasks',p_accepting_tasks
    );
    v_fingerprint char(64);
    v_replay jsonb;
    v_worker public.workforce_workers%ROWTYPE;
    v_correlation text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
BEGIN
    IF p_worker_id NOT IN ('supervisor','haiku','sonnet','opus','data_archivist')
       OR p_accepting_tasks IS NULL
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._~-]{8,128}$' THEN
        RAISE EXCEPTION 'Scheduling command input is invalid';
    END IF;
    v_fingerprint := sireader_workforce_private.command_fingerprint_v1(v_request);
    v_replay := sireader_workforce_private.command_replay_v1(v_actor,p_idempotency_key,v_fingerprint);
    IF v_replay IS NOT NULL THEN RETURN v_replay; END IF;
    SELECT * INTO v_worker FROM public.workforce_workers WHERE worker_id=p_worker_id FOR UPDATE;
    IF NOT FOUND OR NOT v_worker.enabled THEN RAISE EXCEPTION 'WORKER_UNAVAILABLE'; END IF;
    IF v_worker.accepting_tasks IS DISTINCT FROM p_accepting_tasks THEN
        UPDATE public.workforce_workers SET accepting_tasks=p_accepting_tasks,
            updated_at=pg_catalog.clock_timestamp() WHERE worker_id=p_worker_id;
        PERFORM sireader_workforce_private.append_operator_event_v1(
            'WORKER_STATE_CHANGED',v_actor,NULL,p_worker_id,v_correlation,
            pg_catalog.jsonb_build_object('field','accepting_tasks',
                'previous',v_worker.accepting_tasks,'current',p_accepting_tasks)
        );
    END IF;
    RETURN sireader_workforce_private.command_record_v1(
        v_actor,'WORKER_SCHEDULING_SET',p_worker_id,p_idempotency_key,v_fingerprint,
        v_correlation,pg_catalog.jsonb_build_object(
            'accepted',true,'worker_id',p_worker_id,'accepting_tasks',p_accepting_tasks,
            'schedule_state',CASE WHEN p_accepting_tasks THEN 'ACCEPTING' ELSE 'PAUSED' END
        )
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.command_set_worker_scheduling_v1(text,boolean,text)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.command_set_worker_scheduling_v1(text,boolean,text) FROM PUBLIC;

-- Trusted review reads: identity is supplied by the DB login, not a request header.
CREATE OR REPLACE FUNCTION sireader_workforce_private.review_get_pending_v1(p_review_id text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('REVIEW');
    v_result jsonb;
BEGIN
    SELECT pg_catalog.jsonb_build_object(
        'review_id',r.review_id,'task_id',r.task_id,'attempt_id',r.attempt_id,
        'status',r.status,'original_output_sha256',r.original_output_sha256,
        'review_expires_at',r.review_expires_at,'created_at',r.created_at,
        'task',pg_catalog.jsonb_build_object(
            'task_id',t.task_id,'title',t.title,'task_type',t.task_type,
            'required_capability',t.required_capability,'requested_worker_id',t.requested_worker_id,
            'assigned_worker_id',t.assigned_worker_id,'correlation_id',t.correlation_id
        ),
        'original_output',p.original_output
    ) INTO v_result
    FROM public.workforce_task_reviews r
    JOIN public.workforce_tasks t ON t.task_id=r.task_id
    JOIN public.workforce_task_review_payloads p ON p.review_id=r.review_id
    WHERE r.status='PENDING' AND p.payload_purged_at IS NULL
      AND (p_review_id IS NULL OR r.review_id=p_review_id)
    ORDER BY r.created_at ASC
    LIMIT 1;
    RETURN v_result;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.review_get_pending_v1(text) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.review_get_pending_v1(text) FROM PUBLIC;

-- Replace the release trigger predicate with the exact dedicated review authority.
CREATE OR REPLACE FUNCTION sireader_workforce_private.guard_worker_result_release_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
    v_review_status text;
    v_original jsonb;
    v_adjusted jsonb;
    v_purged_at timestamptz;
    v_principal text := pg_catalog.split_part(session_user::text,'.',1);
BEGIN
    IF NEW.result_payload IS NOT DISTINCT FROM OLD.result_payload THEN RETURN NEW; END IF;
    IF NEW.result_payload IS NULL THEN
        IF OLD.result_payload IS NOT NULL
           AND NEW.payload_purged_at IS NOT NULL
           AND NEW.payload_expires_at IS NOT NULL
           AND NEW.payload_expires_at <= pg_catalog.clock_timestamp()
           AND v_principal IN ('sireader_workforce_maint','postgres') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Task result may only be cleared by an authorized retention boundary';
    END IF;
    IF OLD.result_payload IS NOT NULL THEN RAISE EXCEPTION 'Task result publication is write-once'; END IF;
    IF NEW.status <> 'COMPLETED' THEN RAISE EXCEPTION 'Task result may only be published for a completed task'; END IF;

    SELECT p.worker_id INTO v_worker_id
    FROM sireader_workforce_private.worker_principals p
    WHERE p.database_role=v_principal::name AND p.enabled;
    IF v_worker_id IS NOT NULL THEN
        IF OLD.status <> 'RUNNING' OR OLD.lease_token IS NULL OR OLD.lease_expires_at IS NULL
           OR OLD.lease_expires_at <= pg_catalog.clock_timestamp()
           OR OLD.assigned_worker_id IS DISTINCT FROM v_worker_id
           OR NEW.lease_token IS NOT NULL OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR EXISTS (SELECT 1 FROM public.workforce_task_reviews r WHERE r.task_id=OLD.task_id)
           OR NOT EXISTS (
               SELECT 1 FROM public.workforce_task_attempts a
               WHERE a.task_id=OLD.task_id AND a.lease_token=OLD.lease_token
                 AND a.worker_id=v_worker_id AND a.status='COMPLETED'
                 AND a.finished_at=NEW.completed_at
           ) THEN
            RAISE EXCEPTION 'Worker result is not bound to the previous active lease and matching completed attempt';
        END IF;
        RETURN NEW;
    END IF;

    IF v_principal='sireader_workforce_review' THEN
        SELECT r.status,p.original_output,p.adjusted_output,p.payload_purged_at
          INTO v_review_status,v_original,v_adjusted,v_purged_at
          FROM public.workforce_task_reviews r
          JOIN public.workforce_task_review_payloads p ON p.review_id=r.review_id
         WHERE r.task_id=NEW.task_id;
        IF v_purged_at IS NULL
           AND ((v_review_status='APPROVED' AND NEW.result_payload IS NOT DISTINCT FROM v_original)
             OR (v_review_status='EDITED' AND NEW.result_payload IS NOT DISTINCT FROM v_adjusted)) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'Task result release requires an authenticated worker completion or trusted review operator';
END;
$function$;
ALTER FUNCTION sireader_workforce_private.guard_worker_result_release_v1() OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.guard_worker_result_release_v1() FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.review_adjudicate_v1(
    p_review_id text,p_decision text,p_expected_original_sha256 text,
    p_adjusted_output jsonb,p_reason_code text,p_notes text,p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('REVIEW');
    v_request jsonb;
    v_fingerprint char(64);
    v_replay jsonb;
    v_review public.workforce_task_reviews%ROWTYPE;
    v_payload public.workforce_task_review_payloads%ROWTYPE;
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_adjusted jsonb;
    v_adjusted_sha char(64);
    v_release jsonb;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_correlation text;
BEGIN
    IF p_review_id IS NULL OR p_review_id !~ '^rev_[A-Za-z0-9_-]{8,60}$'
       OR p_decision IS NULL OR p_decision NOT IN ('APPROVED','EDITED','REJECTED')
       OR p_expected_original_sha256 IS NULL OR p_expected_original_sha256 !~ '^[0-9a-f]{64}$'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._~-]{8,128}$'
       OR (p_decision='EDITED' AND p_adjusted_output IS NULL)
       OR (p_decision<>'EDITED' AND p_adjusted_output IS NOT NULL)
       OR (p_decision='REJECTED' AND (p_reason_code IS NULL OR pg_catalog.length(p_reason_code)>48))
       OR (p_notes IS NOT NULL AND pg_catalog.length(p_notes)>500) THEN
        RAISE EXCEPTION 'Review adjudication input is invalid';
    END IF;
    IF p_adjusted_output IS NOT NULL AND
       (pg_catalog.jsonb_typeof(p_adjusted_output)<>'object'
        OR pg_catalog.octet_length(sireader_workforce_private.canonical_json_v1(p_adjusted_output))>65536) THEN
        RAISE EXCEPTION 'Edited review output must be an object no larger than 64 KiB';
    END IF;
    v_request := pg_catalog.jsonb_build_object(
        'command_type','REVIEW_ADJUDICATE',
        'review_id',p_review_id,'decision',p_decision,
        'expected_original_sha256',p_expected_original_sha256,
        'adjusted_output',p_adjusted_output,'reason_code',p_reason_code,'notes',p_notes
    );
    v_fingerprint := sireader_workforce_private.command_fingerprint_v1(v_request);
    v_replay := sireader_workforce_private.command_replay_v1(v_actor,p_idempotency_key,v_fingerprint);
    IF v_replay IS NOT NULL THEN RETURN v_replay; END IF;

    SELECT * INTO v_review FROM public.workforce_task_reviews
    WHERE review_id=p_review_id FOR UPDATE;
    IF NOT FOUND OR v_review.status<>'PENDING' THEN RAISE EXCEPTION 'Review is not pending'; END IF;
    IF v_review.original_output_sha256::text<>p_expected_original_sha256 THEN
        RAISE EXCEPTION 'REVIEW_HASH_CONFLICT';
    END IF;
    SELECT * INTO v_payload FROM public.workforce_task_review_payloads
    WHERE review_id=p_review_id FOR UPDATE;
    IF NOT FOUND OR v_payload.payload_purged_at IS NOT NULL OR v_payload.original_output IS NULL THEN
        RAISE EXCEPTION 'Review payload is unavailable';
    END IF;
    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=v_review.task_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts WHERE attempt_id=v_review.attempt_id FOR UPDATE;
    IF v_task.status<>'COMPLETED' OR v_attempt.task_id<>v_task.task_id OR v_attempt.status<>'COMPLETED' THEN
        RAISE EXCEPTION 'Review is not linked to a completed task attempt';
    END IF;
    v_correlation := v_task.correlation_id;
    IF p_decision='EDITED' THEN
        v_adjusted := sireader_workforce_private.sanitize_review_payload_v1(p_adjusted_output);
        v_adjusted_sha := pg_catalog.encode(
            extensions.digest(
                pg_catalog.convert_to(sireader_workforce_private.canonical_json_v1(v_adjusted),'UTF8'),
                'sha256'
            ),'hex'
        )::char(64);
        UPDATE public.workforce_task_reviews SET status='EDITED',
            adjusted_output_sha256=v_adjusted_sha,adjudicated_by=v_actor,
            adjudicated_at=v_now,reason_code=NULL,notes=p_notes,updated_at=v_now
        WHERE review_id=p_review_id;
        UPDATE public.workforce_task_review_payloads SET adjusted_output=v_adjusted,updated_at=v_now
        WHERE review_id=p_review_id;
        v_release := v_adjusted;
    ELSIF p_decision='APPROVED' THEN
        UPDATE public.workforce_task_reviews SET status='APPROVED',
            adjudicated_by=v_actor,adjudicated_at=v_now,reason_code=NULL,notes=p_notes,updated_at=v_now
        WHERE review_id=p_review_id;
        v_release := v_payload.original_output;
    ELSE
        UPDATE public.workforce_task_reviews SET status='REJECTED',
            adjudicated_by=v_actor,adjudicated_at=v_now,reason_code=p_reason_code,
            notes=p_notes,updated_at=v_now WHERE review_id=p_review_id;
        v_release := NULL;
    END IF;

    IF v_release IS NOT NULL THEN
        UPDATE public.workforce_tasks SET result_payload=v_release,updated_at=v_now
        WHERE task_id=v_task.task_id AND result_payload IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'Task result is already released or task state changed'; END IF;
    END IF;
    PERFORM sireader_workforce_private.append_operator_event_v1(
        CASE p_decision WHEN 'APPROVED' THEN 'HUMAN_REVIEW_APPROVED'
                        WHEN 'EDITED' THEN 'HUMAN_REVIEW_EDITED'
                        ELSE 'HUMAN_REVIEW_REJECTED' END,
        v_actor,v_task.task_id,v_attempt.worker_id,v_correlation,
        pg_catalog.jsonb_build_object('review_id',p_review_id,'decision',p_decision,
            'original_output_sha256',v_review.original_output_sha256,
            'adjusted_output_sha256',v_adjusted_sha)
        ,v_attempt.attempt_id
    );
    RETURN sireader_workforce_private.command_record_v1(
        v_actor,'REVIEW_ADJUDICATE',p_review_id,p_idempotency_key,v_fingerprint,
        v_correlation,pg_catalog.jsonb_build_object(
            'accepted',true,'review_id',p_review_id,'task_id',v_task.task_id,
            'decision',p_decision,'adjudicated_by',v_actor,'adjudicated_at',v_now,
            'task_status',v_task.status
        )
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.review_adjudicate_v1(text,text,text,jsonb,text,text,text)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.review_adjudicate_v1(text,text,text,jsonb,text,text,text) FROM PUBLIC;

-- Bounded maintenance helpers are private and invokable only through run_once.
CREATE OR REPLACE FUNCTION sireader_workforce_private.maintenance_recover_leases_impl_v1(
    p_batch_size integer,p_now timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_counts jsonb := pg_catalog.jsonb_build_object('requeued',0,'waiting_review',0,'dead_lettered',0);
    v_status text;
    v_event text;
    v_backoff integer;
BEGIN
    FOR v_task IN
        SELECT * FROM public.workforce_tasks
        WHERE status IN ('CLAIMED','RUNNING')
          AND lease_expires_at IS NOT NULL AND lease_expires_at<=p_now
        ORDER BY lease_expires_at,created_at,task_id
        LIMIT p_batch_size FOR UPDATE SKIP LOCKED
    LOOP
        SELECT * INTO v_attempt FROM public.workforce_task_attempts
        WHERE task_id=v_task.task_id AND lease_token=v_task.lease_token
        FOR UPDATE;
        IF FOUND AND (v_attempt.provider_request_id IS NOT NULL
                      OR v_attempt.provider_started_at IS NOT NULL
                      OR v_attempt.status IN ('PROVIDER_REQUESTED','UNKNOWN')) THEN
            v_status := 'WAITING_REVIEW'; v_event := 'TASK_WAITING_REVIEW';
            UPDATE public.workforce_task_attempts SET status='UNKNOWN',
                error_code='LEASE_EXPIRED_AFTER_PROVIDER_REQUEST',finished_at=p_now
            WHERE attempt_id=v_attempt.attempt_id;
            v_counts := pg_catalog.jsonb_set(v_counts,'{waiting_review}',
                pg_catalog.to_jsonb((v_counts->>'waiting_review')::integer+1));
        ELSIF v_task.attempts < v_task.max_attempts
              AND (v_task.deadline_at IS NULL OR v_task.deadline_at>p_now) THEN
            v_status := 'RETRY_WAIT'; v_event := 'TASK_RETRIED';
            v_backoff := least(3600,greatest(1,30 * (2 ^ least(10,greatest(0,v_task.attempts-1)))));
            UPDATE public.workforce_task_attempts SET status='TIMED_OUT',
                error_code='LEASE_EXPIRED_BEFORE_PROVIDER',finished_at=p_now
            WHERE attempt_id=v_attempt.attempt_id;
            UPDATE public.workforce_tasks SET not_before=p_now+pg_catalog.make_interval(secs=>v_backoff)
            WHERE task_id=v_task.task_id;
            v_counts := pg_catalog.jsonb_set(v_counts,'{requeued}',
                pg_catalog.to_jsonb((v_counts->>'requeued')::integer+1));
        ELSE
            v_status := 'DEAD_LETTERED'; v_event := 'TASK_DEAD_LETTERED';
            UPDATE public.workforce_task_attempts SET status='TIMED_OUT',
                error_code='LEASE_EXPIRED_MAX_ATTEMPTS',finished_at=p_now
            WHERE attempt_id=v_attempt.attempt_id;
            v_counts := pg_catalog.jsonb_set(v_counts,'{dead_lettered}',
                pg_catalog.to_jsonb((v_counts->>'dead_lettered')::integer+1));
        END IF;
        UPDATE public.workforce_tasks SET status=v_status,
            failure_code=CASE WHEN v_status='WAITING_REVIEW' THEN 'LEASE_EXPIRED_AFTER_PROVIDER_REQUEST'
                              WHEN v_status='RETRY_WAIT' THEN 'LEASE_EXPIRED_BEFORE_PROVIDER'
                              ELSE 'LEASE_EXPIRED_MAX_ATTEMPTS' END,
            payload_expires_at=CASE WHEN v_status='WAITING_REVIEW' THEN NULL ELSE p_now+interval '30 days' END,
            lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=p_now
        WHERE task_id=v_task.task_id;
        PERFORM sireader_workforce_private.append_operator_event_v1(
            v_event,'system',v_task.task_id,v_task.assigned_worker_id,v_task.correlation_id,
            pg_catalog.jsonb_build_object('provider_outcome_unknown',
                COALESCE(v_attempt.provider_request_id IS NOT NULL OR v_attempt.status IN ('PROVIDER_REQUESTED','UNKNOWN'),false))
            ,v_attempt.attempt_id
        );
    END LOOP;
    RETURN v_counts;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.maintenance_recover_leases_impl_v1(integer,timestamptz)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.maintenance_recover_leases_impl_v1(integer,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.maintenance_expire_deadlines_impl_v1(
    p_batch_size integer,p_now timestamptz
)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE v_task public.workforce_tasks%ROWTYPE; v_count integer := 0;
BEGIN
    FOR v_task IN
        SELECT * FROM public.workforce_tasks
        WHERE status IN ('QUEUED','RETRY_WAIT') AND deadline_at IS NOT NULL AND deadline_at<=p_now
        ORDER BY deadline_at,task_id LIMIT p_batch_size FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE public.workforce_tasks SET status='DEAD_LETTERED',failure_code='DEADLINE_EXPIRED',
            payload_expires_at=p_now+interval '30 days',updated_at=p_now
        WHERE task_id=v_task.task_id;
        PERFORM sireader_workforce_private.append_operator_event_v1(
            'TASK_DEAD_LETTERED','system',v_task.task_id,NULL,v_task.correlation_id,
            pg_catalog.jsonb_build_object('error_code','DEADLINE_EXPIRED')
        );
        v_count := v_count+1;
    END LOOP;
    RETURN v_count;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.maintenance_expire_deadlines_impl_v1(integer,timestamptz)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.maintenance_expire_deadlines_impl_v1(integer,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.maintenance_expire_reviews_impl_v1(
    p_batch_size integer,p_now timestamptz
)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE v_review public.workforce_task_reviews%ROWTYPE; v_task public.workforce_tasks%ROWTYPE; v_count integer := 0;
BEGIN
    FOR v_review IN
        SELECT * FROM public.workforce_task_reviews
        WHERE status='PENDING' AND review_expires_at<=p_now
        ORDER BY review_expires_at,review_id LIMIT p_batch_size FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE public.workforce_task_reviews SET status='EXPIRED',adjudicated_by='system',
            adjudicated_at=p_now,reason_code='REVIEW_TIMED_OUT',updated_at=p_now
        WHERE review_id=v_review.review_id;
        SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=v_review.task_id FOR UPDATE;
        PERFORM sireader_workforce_private.append_operator_event_v1(
            'HUMAN_REVIEW_EXPIRED','system',v_review.task_id,NULL,v_task.correlation_id,
            pg_catalog.jsonb_build_object('review_id',v_review.review_id,'reason_code','REVIEW_TIMED_OUT'),
            v_review.attempt_id
        );
        v_count := v_count+1;
    END LOOP;
    RETURN v_count;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.maintenance_expire_reviews_impl_v1(integer,timestamptz)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.maintenance_expire_reviews_impl_v1(integer,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.maintenance_purge_task_payloads_impl_v1(
    p_batch_size integer,p_now timestamptz
)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE v_task public.workforce_tasks%ROWTYPE; v_count integer := 0;
BEGIN
    FOR v_task IN
        SELECT t.* FROM public.workforce_tasks t
        WHERE t.status IN ('WAITING_REVIEW','COMPLETED','FAILED','CANCELLED','DEAD_LETTERED')
          AND t.payload_expires_at IS NOT NULL AND t.payload_expires_at<=p_now
          AND t.payload_purged_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM public.workforce_task_reviews r
              WHERE r.task_id=t.task_id AND r.status='PENDING'
          )
        ORDER BY t.payload_expires_at,t.task_id
        LIMIT p_batch_size FOR UPDATE OF t SKIP LOCKED
    LOOP
        UPDATE public.workforce_tasks SET input_payload='{}'::jsonb,result_payload=NULL,
            payload_purged_at=p_now,updated_at=p_now WHERE task_id=v_task.task_id;
        v_count := v_count+1;
    END LOOP;
    RETURN v_count;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.maintenance_purge_task_payloads_impl_v1(integer,timestamptz)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.maintenance_purge_task_payloads_impl_v1(integer,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.maintenance_purge_review_payloads_impl_v1(
    p_batch_size integer,p_now timestamptz
)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE v_payload public.workforce_task_review_payloads%ROWTYPE; v_count integer := 0;
BEGIN
    FOR v_payload IN
        SELECT p.* FROM public.workforce_task_review_payloads p
        JOIN public.workforce_task_reviews r ON r.review_id=p.review_id
        JOIN public.workforce_tasks t ON t.task_id=r.task_id
        WHERE r.status IN ('APPROVED','EDITED','REJECTED','EXPIRED')
          AND p.payload_purged_at IS NULL AND t.payload_expires_at IS NOT NULL
          AND t.payload_expires_at<=p_now
        ORDER BY t.payload_expires_at,p.review_id
        LIMIT p_batch_size FOR UPDATE OF p SKIP LOCKED
    LOOP
        UPDATE public.workforce_task_review_payloads SET original_output=NULL,
            adjusted_output=NULL,payload_purged_at=p_now,updated_at=p_now
        WHERE review_id=v_payload.review_id;
        v_count := v_count+1;
    END LOOP;
    RETURN v_count;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.maintenance_purge_review_payloads_impl_v1(integer,timestamptz)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.maintenance_purge_review_payloads_impl_v1(integer,timestamptz) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.maintenance_run_once_v1(
    p_batch_size integer DEFAULT 100,p_now timestamptz DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_actor text := sireader_workforce_private.require_authority_v1('MAINTENANCE');
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_recovery jsonb;
    v_deadlines integer;
    v_reviews integer;
    v_tasks integer;
    v_review_payloads integer;
    v_run_id text := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
    v_started_at timestamptz := pg_catalog.clock_timestamp();
    v_result jsonb;
BEGIN
    IF p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'Maintenance batch size must be between 1 and 500';
    END IF;
    IF p_now IS NOT NULL THEN
        RAISE EXCEPTION 'Caller-supplied maintenance timestamp is not permitted; database time is authoritative';
    END IF;
    v_recovery := sireader_workforce_private.maintenance_recover_leases_impl_v1(p_batch_size,v_now);
    v_deadlines := sireader_workforce_private.maintenance_expire_deadlines_impl_v1(p_batch_size,v_now);
    v_reviews := sireader_workforce_private.maintenance_expire_reviews_impl_v1(p_batch_size,v_now);
    v_tasks := sireader_workforce_private.maintenance_purge_task_payloads_impl_v1(p_batch_size,v_now);
    v_review_payloads := sireader_workforce_private.maintenance_purge_review_payloads_impl_v1(p_batch_size,v_now);
    v_result := pg_catalog.jsonb_build_object(
        'leases_recovered_requeued',(v_recovery->>'requeued')::integer,
        'leases_recovered_waiting_review',(v_recovery->>'waiting_review')::integer,
        'leases_recovered_dead_lettered',(v_recovery->>'dead_lettered')::integer,
        'deadline_tasks_expired',v_deadlines,'stale_reviews_expired',v_reviews,
        'task_payloads_purged',v_tasks,'review_payloads_purged',v_review_payloads
    );
    INSERT INTO sireader_workforce_private.authority_maintenance_audit(
        run_id,database_role,actor_id,batch_size,started_at,finished_at,result
    ) VALUES (
        v_run_id,pg_catalog.split_part(session_user::text,'.',1)::name,v_actor,
        p_batch_size,v_started_at,pg_catalog.clock_timestamp(),v_result
    );
    RETURN v_result;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)
OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz) FROM PUBLIC;

-- Internal function owner receives only the additional writes needed by operator/review/maintenance functions.
GRANT INSERT ON public.workforce_tasks TO sireader_workforce_fn_owner;
GRANT UPDATE ON public.workforce_task_reviews, public.workforce_task_review_payloads TO sireader_workforce_fn_owner;
-- Grant only narrow command/read/review/maintenance EXECUTE surfaces.
GRANT EXECUTE ON FUNCTION sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text),
    sireader_workforce_private.command_cancel_task_v1(text,text),
    sireader_workforce_private.command_retry_task_v1(text,text),
    sireader_workforce_private.command_set_worker_scheduling_v1(text,boolean,text)
TO sireader_workforce_cmd;
GRANT EXECUTE ON FUNCTION sireader_workforce_private.review_get_pending_v1(text),
    sireader_workforce_private.review_adjudicate_v1(text,text,text,jsonb,text,text,text)
TO sireader_workforce_review;
GRANT EXECUTE ON FUNCTION sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)
TO sireader_workforce_maint;

REVOKE ALL ON ALL TABLES IN SCHEMA sireader_workforce_private FROM
    sireader_worker_supervisor,sireader_worker_haiku,sireader_worker_sonnet,sireader_worker_opus,
    sireader_worker_decision_engineer,sireader_worker_market_monitor,sireader_worker_data_archivist,
    sireader_workforce_cmd,sireader_workforce_review,sireader_workforce_maint;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA sireader_workforce_private FROM
    sireader_worker_supervisor,sireader_worker_haiku,sireader_worker_sonnet,sireader_worker_opus,
    sireader_worker_decision_engineer,sireader_worker_market_monitor,sireader_worker_data_archivist,
    sireader_workforce_cmd,sireader_workforce_review,sireader_workforce_maint;

COMMIT;
