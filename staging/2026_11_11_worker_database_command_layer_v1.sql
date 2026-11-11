-- SIREADER Worker Database Command Layer v1 — STAGING REHEARSAL ONLY.
-- Hard-pinned to the isolated local PG17 restore. This file is NOT a Supabase migration.
-- It creates seven NOLOGIN worker principals for identity/ACL tests; production
-- LOGIN/password provisioning is deliberately out-of-band and not included.
BEGIN;

DO $guard$
BEGIN
    IF current_database() <> 'sireader_stage'
       OR inet_server_addr() IS DISTINCT FROM inet '127.0.0.1'
       OR inet_server_port() IS DISTINCT FROM 55439
       OR session_user <> 'postgres' THEN
        RAISE EXCEPTION 'Worker command layer rehearsal is restricted to local sireader_stage on 127.0.0.1:55439 as postgres';
    END IF;
    IF to_regclass('public.workforce_workers') IS NULL
       OR to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_task_attempts') IS NULL
       OR to_regclass('public.workforce_activity_events') IS NULL
       OR to_regclass('public.workforce_task_reviews') IS NULL
       OR to_regclass('public.workforce_task_review_payloads') IS NULL THEN
        RAISE EXCEPTION 'Production-like workforce tables are required';
    END IF;
END;
$guard$;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

DO $roles$
DECLARE
    v_role text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_workforce_fn_owner') THEN
        CREATE ROLE sireader_workforce_fn_owner
            NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    FOREACH v_role IN ARRAY ARRAY[
        'sireader_worker_supervisor',
        'sireader_worker_haiku',
        'sireader_worker_sonnet',
        'sireader_worker_opus',
        'sireader_worker_decision_engineer',
        'sireader_worker_market_monitor',
        'sireader_worker_data_archivist'
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
                  AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                       OR rolreplication OR rolbypassrls OR rolinherit)
            ) THEN
                RAISE EXCEPTION 'Existing worker role % violates staging least privilege', v_role;
            END IF;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'sireader_workforce_fn_owner'
          AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
               OR rolreplication OR rolbypassrls OR rolinherit)
    ) THEN
        RAISE EXCEPTION 'Function owner violates least privilege';
    END IF;
END;
$roles$;

GRANT sireader_workforce_fn_owner TO postgres WITH SET TRUE, INHERIT FALSE;

CREATE SCHEMA IF NOT EXISTS sireader_workforce_private AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA sireader_workforce_private FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA sireader_workforce_private TO sireader_workforce_fn_owner;
GRANT USAGE ON SCHEMA sireader_workforce_private TO
    sireader_worker_supervisor, sireader_worker_haiku, sireader_worker_sonnet,
    sireader_worker_opus, sireader_worker_decision_engineer,
    sireader_worker_market_monitor, sireader_worker_data_archivist;

CREATE TABLE IF NOT EXISTS sireader_workforce_private.worker_principals (
    database_role name PRIMARY KEY,
    worker_id varchar(64) NOT NULL UNIQUE
        REFERENCES public.workforce_workers(worker_id) ON DELETE RESTRICT,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
ALTER TABLE sireader_workforce_private.worker_principals OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.worker_principals FROM PUBLIC;

SET ROLE sireader_workforce_fn_owner;

INSERT INTO sireader_workforce_private.worker_principals(database_role, worker_id)
VALUES
    ('sireader_worker_supervisor', 'supervisor'),
    ('sireader_worker_haiku', 'haiku'),
    ('sireader_worker_sonnet', 'sonnet'),
    ('sireader_worker_opus', 'opus'),
    ('sireader_worker_decision_engineer', 'decision_engineer'),
    ('sireader_worker_market_monitor', 'market_monitor'),
    ('sireader_worker_data_archivist', 'data_archivist')
ON CONFLICT (database_role) DO UPDATE
SET worker_id = EXCLUDED.worker_id, enabled = true;

CREATE TABLE IF NOT EXISTS sireader_workforce_private.review_rules (
    capability_id varchar(64) NOT NULL,
    task_type varchar(64) NOT NULL,
    policy_version varchar(32) NOT NULL,
    mode varchar(16) NOT NULL CHECK (mode IN ('MANDATORY','SAMPLE','NONE')),
    sample_rate_percent integer NOT NULL DEFAULT 0 CHECK (sample_rate_percent BETWEEN 0 AND 100),
    sample_salt varchar(64) NOT NULL DEFAULT 'sireader_governance_v1',
    PRIMARY KEY (capability_id, task_type)
);
ALTER TABLE sireader_workforce_private.review_rules OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.review_rules FROM PUBLIC;

INSERT INTO sireader_workforce_private.review_rules
    (capability_id, task_type, policy_version, mode, sample_rate_percent, sample_salt)
VALUES
    ('instruction_intake','INSTRUCTION_INTAKE','2026.11.01','SAMPLE',10,'sireader_governance_v1'),
    ('instruction_intake','ROUTING','2026.11.01','SAMPLE',10,'sireader_governance_v1'),
    ('task_decomposition','TASK_DECOMPOSITION','2026.11.01','SAMPLE',15,'sireader_governance_v1'),
    ('task_decomposition','ROUTING','2026.11.01','SAMPLE',15,'sireader_governance_v1'),
    ('workflow_supervision','WORKFLOW_SUPERVISION','2026.11.01','SAMPLE',20,'sireader_governance_v1'),
    ('workflow_supervision','ROUTING','2026.11.01','SAMPLE',20,'sireader_governance_v1'),
    ('task_assignment_oversight','TASK_ASSIGNMENT_OVERSIGHT','2026.11.01','SAMPLE',15,'sireader_governance_v1'),
    ('task_assignment_oversight','ROUTING','2026.11.01','SAMPLE',15,'sireader_governance_v1'),
    ('result_review','RESULT_REVIEW','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('result_review','REVIEW','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('quick_research','QUICK_RESEARCH','2026.11.01','SAMPLE',20,'sireader_governance_v1'),
    ('quick_research','RESEARCH','2026.11.01','SAMPLE',20,'sireader_governance_v1'),
    ('information_extraction','INFORMATION_EXTRACTION','2026.11.01','SAMPLE',10,'sireader_governance_v1'),
    ('information_extraction','CLASSIFICATION','2026.11.01','SAMPLE',10,'sireader_governance_v1'),
    ('summarization','SUMMARIZATION','2026.11.01','SAMPLE',15,'sireader_governance_v1'),
    ('summarization','CLASSIFICATION','2026.11.01','SAMPLE',15,'sireader_governance_v1'),
    ('lightweight_classification','LIGHTWEIGHT_CLASSIFICATION','2026.11.01','SAMPLE',10,'sireader_governance_v1'),
    ('lightweight_classification','CLASSIFICATION','2026.11.01','SAMPLE',10,'sireader_governance_v1'),
    ('data_analysis','DATA_ANALYSIS','2026.11.01','SAMPLE',25,'sireader_governance_v1'),
    ('data_analysis','REASONING','2026.11.01','SAMPLE',25,'sireader_governance_v1'),
    ('comparison','COMPARISON','2026.11.01','SAMPLE',20,'sireader_governance_v1'),
    ('comparison','REASONING','2026.11.01','SAMPLE',20,'sireader_governance_v1'),
    ('evaluation','EVALUATION','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('evaluation','REASONING','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('structured_reasoning','STRUCTURED_REASONING','2026.11.01','SAMPLE',30,'sireader_governance_v1'),
    ('structured_reasoning','REASONING','2026.11.01','SAMPLE',30,'sireader_governance_v1'),
    ('complex_reasoning','COMPLEX_REASONING','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('complex_reasoning','ESCALATION','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('strategic_analysis','STRATEGIC_ANALYSIS','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('strategic_analysis','ESCALATION','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('deep_review','DEEP_REVIEW','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('deep_review','ESCALATION','2026.11.01','MANDATORY',0,'sireader_governance_v1'),
    ('data_archiving','DATA_ARCHIVING','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('data_archiving','ARCHIVAL','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('artifact_indexing','ARTIFACT_INDEXING','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('artifact_indexing','ARCHIVAL','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('historical_retrieval_simulation','HISTORICAL_RETRIEVAL_SIMULATION','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('historical_retrieval_simulation','ARCHIVAL','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('data_organization','DATA_ORGANIZATION','2026.11.01','NONE',0,'sireader_governance_v1'),
    ('data_organization','ARCHIVAL','2026.11.01','NONE',0,'sireader_governance_v1')
ON CONFLICT (capability_id, task_type) DO UPDATE
SET policy_version = EXCLUDED.policy_version,
    mode = EXCLUDED.mode,
    sample_rate_percent = EXCLUDED.sample_rate_percent,
    sample_salt = EXCLUDED.sample_salt;

RESET ROLE;

-- Internal function owner gets only the workforce tables required by worker commands.
GRANT USAGE ON SCHEMA public TO sireader_workforce_fn_owner;
GRANT USAGE ON SCHEMA extensions TO sireader_workforce_fn_owner;
GRANT SELECT, UPDATE ON public.workforce_workers TO sireader_workforce_fn_owner;
GRANT SELECT ON public.workforce_capabilities, public.workforce_worker_capabilities TO sireader_workforce_fn_owner;
GRANT SELECT, INSERT, UPDATE ON public.workforce_heartbeats TO sireader_workforce_fn_owner;
GRANT SELECT, UPDATE ON public.workforce_tasks TO sireader_workforce_fn_owner;
GRANT SELECT, INSERT, UPDATE ON public.workforce_task_attempts TO sireader_workforce_fn_owner;
GRANT INSERT ON public.workforce_activity_events TO sireader_workforce_fn_owner;
GRANT SELECT, INSERT ON public.workforce_task_reviews,
    public.workforce_task_review_payloads
TO sireader_workforce_fn_owner;

DO $seq$
DECLARE
    v_sequence text;
BEGIN
    v_sequence := pg_catalog.pg_get_serial_sequence('public.workforce_activity_events', 'event_seq');
    IF v_sequence IS NULL THEN
        RAISE EXCEPTION 'workforce activity event identity sequence not found';
    END IF;
    EXECUTE pg_catalog.format('GRANT USAGE, SELECT ON SEQUENCE %s TO sireader_workforce_fn_owner', v_sequence);
END;
$seq$;

-- Explicit RLS path for the NOLOGIN function owner; worker logins have no table policies.
DO $policies$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'workforce_workers','workforce_capabilities','workforce_worker_capabilities',
        'workforce_heartbeats','workforce_tasks','workforce_task_attempts',
        'workforce_activity_events','workforce_task_reviews','workforce_task_review_payloads'
    ] LOOP
        EXECUTE pg_catalog.format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE pg_catalog.format('DROP POLICY IF EXISTS workforce_fn_owner_all ON public.%I', v_table);
        EXECUTE pg_catalog.format(
            'CREATE POLICY workforce_fn_owner_all ON public.%I FOR ALL TO sireader_workforce_fn_owner USING (true) WITH CHECK (true)',
            v_table
        );
    END LOOP;
END;
$policies$;

-- Worker logins receive no workforce base-table privileges, public-schema CREATE,
-- or workforce sequence grants. Do not touch pre-existing API views owned by
-- sireader_view_owner: these newly-created roles were never granted view access.
REVOKE ALL ON TABLE
    public.workforce_workers,
    public.workforce_capabilities,
    public.workforce_worker_capabilities,
    public.workforce_heartbeats,
    public.workforce_tasks,
    public.workforce_task_attempts,
    public.workforce_activity_events,
    public.workforce_task_reviews,
    public.workforce_task_review_payloads
FROM sireader_worker_supervisor, sireader_worker_haiku, sireader_worker_sonnet,
    sireader_worker_opus, sireader_worker_decision_engineer,
    sireader_worker_market_monitor, sireader_worker_data_archivist;
REVOKE ALL ON SCHEMA public FROM
    sireader_worker_supervisor, sireader_worker_haiku, sireader_worker_sonnet,
    sireader_worker_opus, sireader_worker_decision_engineer,
    sireader_worker_market_monitor, sireader_worker_data_archivist;

DO $worker_sequence_revoke$
DECLARE v_sequence text;
BEGIN
    v_sequence := pg_catalog.pg_get_serial_sequence('public.workforce_activity_events','event_seq');
    IF v_sequence IS NULL THEN
        RAISE EXCEPTION 'workforce activity event identity sequence not found for worker ACL cleanup';
    END IF;
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON SEQUENCE %s FROM sireader_worker_supervisor,sireader_worker_haiku,sireader_worker_sonnet,sireader_worker_opus,sireader_worker_decision_engineer,sireader_worker_market_monitor,sireader_worker_data_archivist',
        v_sequence
    );
END;
$worker_sequence_revoke$;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_identity_v1(p_worker_id text)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
BEGIN
    SELECT p.worker_id
      INTO v_worker_id
      FROM sireader_workforce_private.worker_principals AS p
     WHERE p.database_role = pg_catalog.split_part(session_user::text, '.', 1)::name
       AND p.enabled;

    IF v_worker_id IS NULL THEN
        RAISE EXCEPTION 'Database identity is not mapped to an enabled workforce worker';
    END IF;
    IF p_worker_id IS NOT NULL AND p_worker_id IS DISTINCT FROM v_worker_id THEN
        RAISE EXCEPTION 'Supplied worker identity does not match authenticated database identity';
    END IF;
    RETURN v_worker_id;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_identity_v1(text) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_identity_v1(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.canonical_json_v1(p_value jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog
AS $function$
DECLARE
    v_type text;
    v_result text;
BEGIN
    v_type := pg_catalog.jsonb_typeof(p_value);
    IF p_value IS NULL THEN
        RETURN 'null';
    ELSIF v_type = 'object' THEN
        SELECT '{' || COALESCE(
            pg_catalog.string_agg(
                pg_catalog.to_json(k)::text || ':' ||
                sireader_workforce_private.canonical_json_v1(v),
                ',' ORDER BY k
            ), ''
        ) || '}'
          INTO v_result
          FROM pg_catalog.jsonb_each(p_value) AS item(k, v);
        RETURN v_result;
    ELSIF v_type = 'array' THEN
        SELECT '[' || COALESCE(
            pg_catalog.string_agg(
                sireader_workforce_private.canonical_json_v1(value),
                ',' ORDER BY ord
            ), ''
        ) || ']'
          INTO v_result
          FROM pg_catalog.jsonb_array_elements(p_value) WITH ORDINALITY AS item(value, ord);
        RETURN v_result;
    ELSIF v_type = 'string' THEN
        RETURN pg_catalog.to_json(p_value #>> '{}')::text;
    ELSE
        RETURN p_value::text;
    END IF;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.canonical_json_v1(jsonb) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.canonical_json_v1(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.sanitize_review_payload_v1(p_value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog
AS $function$
DECLARE
    v_type text;
    v_result jsonb;
    v_key text;
    v_value jsonb;
    v_text text;
BEGIN
    v_type := pg_catalog.jsonb_typeof(p_value);
    IF v_type = 'object' THEN
        v_result := '{}'::jsonb;
        FOR v_key, v_value IN SELECT key, value FROM pg_catalog.jsonb_each(p_value) LOOP
            IF v_key ILIKE '%token%' OR v_key ILIKE '%secret%' OR v_key ILIKE '%password%'
               OR v_key ILIKE '%key%' OR v_key ILIKE '%authorization%' OR v_key ILIKE '%cookie%'
               OR v_key ILIKE '%credential%' OR v_key ILIKE '%connection_string%' THEN
                v_result := v_result || pg_catalog.jsonb_build_object(v_key, '[REDACTED]');
            ELSE
                v_result := v_result || pg_catalog.jsonb_build_object(
                    v_key, sireader_workforce_private.sanitize_review_payload_v1(v_value)
                );
            END IF;
        END LOOP;
        RETURN v_result;
    ELSIF v_type = 'array' THEN
        SELECT COALESCE(
            pg_catalog.jsonb_agg(sireader_workforce_private.sanitize_review_payload_v1(value) ORDER BY ord),
            '[]'::jsonb
        )
          INTO v_result
          FROM pg_catalog.jsonb_array_elements(p_value) WITH ORDINALITY AS item(value, ord);
        RETURN v_result;
    ELSIF v_type = 'string' THEN
        v_text := p_value #>> '{}';
        v_text := pg_catalog.regexp_replace(v_text, '(?i)\mBearer\s+[A-Za-z0-9_.=-]+', '[REDACTED]', 'g');
        v_text := pg_catalog.regexp_replace(v_text, '(?i)\m(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?):\/\/[^\s@]+@[^\s]+', '[REDACTED]', 'g');
        v_text := pg_catalog.regexp_replace(v_text, '\m(?:AIza[0-9A-Za-z_-]{30,45}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_-]{20,})\M', '[REDACTED]', 'g');
        RETURN pg_catalog.to_jsonb(v_text);
    END IF;
    RETURN p_value;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.sanitize_review_payload_v1(jsonb) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.sanitize_review_payload_v1(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.contains_personal_data_v1(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog
AS $function$
DECLARE
    v_type text;
    v_item record;
    v_key text;
    v_markers text[] := ARRAY[
        'email','phone','mobile','passport','national_id','personal_id','identity_number',
        'date_of_birth','home_address','address','postal_code','zip_code','ssn',
        'social_security','tax_id','full_name','first_name','last_name'
    ];
BEGIN
    v_type := pg_catalog.jsonb_typeof(p_value);
    IF v_type = 'object' THEN
        FOR v_item IN SELECT key, value FROM pg_catalog.jsonb_each(p_value) LOOP
            FOREACH v_key IN ARRAY v_markers LOOP
                IF pg_catalog.strpos(pg_catalog.lower(v_item.key), v_key) > 0 THEN
                    RETURN true;
                END IF;
            END LOOP;
            IF sireader_workforce_private.contains_personal_data_v1(v_item.value) THEN
                RETURN true;
            END IF;
        END LOOP;
    ELSIF v_type = 'array' THEN
        FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_value) LOOP
            IF sireader_workforce_private.contains_personal_data_v1(v_item.value) THEN
                RETURN true;
            END IF;
        END LOOP;
    ELSIF v_type = 'string' THEN
        RETURN (p_value #>> '{}') ~* '\m[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\M';
    END IF;
    RETURN false;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.contains_personal_data_v1(jsonb) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.contains_personal_data_v1(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.requires_review_v1(
    p_capability_id text, p_task_type text, p_task_id text, p_title text, p_input jsonb
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog
AS $function$
DECLARE
    v_rule record;
    v_hash_hex text;
    v_hash_value numeric := 0;
    v_bucket integer;
    v_i integer;
BEGIN
    IF p_capability_id IN (
        'rule_validation','decision_evaluation','risk_assessment_simulation',
        'decision_consistency_checks','market_monitoring_simulation',
        'market_condition_detection','anomaly_detection_simulation','market_event_reporting'
    ) THEN
        RETURN false;
    END IF;
    IF sireader_workforce_private.contains_personal_data_v1(p_input)
       OR (COALESCE(p_title,'') ~* '\m[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\M') THEN
        RETURN true;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_capabilities c
        WHERE c.capability_id = p_capability_id
          AND c.enabled
          AND c.task_types ? p_task_type
    ) THEN
        RETURN false;
    END IF;
    SELECT * INTO v_rule
    FROM sireader_workforce_private.review_rules r
    WHERE r.capability_id = p_capability_id AND r.task_type = p_task_type;
    IF NOT FOUND OR v_rule.mode = 'NONE' THEN
        RETURN false;
    ELSIF v_rule.mode = 'MANDATORY' THEN
        RETURN true;
    ELSIF v_rule.mode = 'SAMPLE' THEN
        v_hash_hex := pg_catalog.encode(
            extensions.digest(
                pg_catalog.convert_to(v_rule.policy_version || ':' || p_task_id || ':' || v_rule.sample_salt, 'UTF8'),
                'sha256'
            ),
            'hex'
        );
        FOR v_i IN 1..pg_catalog.length(v_hash_hex) LOOP
            v_hash_value := (v_hash_value * 16)
                + pg_catalog.strpos('0123456789abcdef', pg_catalog.substr(v_hash_hex, v_i, 1)) - 1;
        END LOOP;
        v_bucket := pg_catalog.mod(v_hash_value, 100)::integer;
        RETURN v_bucket < v_rule.sample_rate_percent;
    END IF;
    RETURN false;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.requires_review_v1(text,text,text,text,jsonb) OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.requires_review_v1(text,text,text,text,jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.append_worker_event_v1(
    p_event_type text, p_worker_id text, p_task_id text, p_attempt_id text,
    p_correlation_id text, p_payload jsonb, p_system_review_event boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_system_review_event THEN
        IF p_event_type <> 'HUMAN_REVIEW_QUEUED' THEN
            RAISE EXCEPTION 'Worker system event type is not allowed';
        END IF;
        INSERT INTO public.workforce_activity_events(
            event_id,event_type,occurred_at,actor_type,actor_id,worker_id,task_id,attempt_id,correlation_id,payload
        ) VALUES (
            pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-',''),
            p_event_type,pg_catalog.clock_timestamp(),'SYSTEM','system',
            p_worker_id,p_task_id,p_attempt_id,p_correlation_id,COALESCE(p_payload,'{}'::jsonb)
        );
    ELSE
        IF p_event_type NOT IN (
            'TASK_CLAIMED','TASK_STARTED','PROVIDER_REQUESTED','TASK_COMPLETED',
            'TASK_FAILED','TASK_RETRIED','TASK_WAITING_REVIEW','TASK_DEAD_LETTERED'
        ) THEN
            RAISE EXCEPTION 'Worker event type is not allowed';
        END IF;
        INSERT INTO public.workforce_activity_events(
            event_id,event_type,occurred_at,actor_type,actor_id,worker_id,task_id,attempt_id,correlation_id,payload
        ) VALUES (
            pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-',''),
            p_event_type,pg_catalog.clock_timestamp(),'WORKER',p_worker_id,
            p_worker_id,p_task_id,p_attempt_id,p_correlation_id,COALESCE(p_payload,'{}'::jsonb)
        );
    END IF;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.append_worker_event_v1(text,text,text,text,text,jsonb,boolean)
    OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.append_worker_event_v1(text,text,text,text,text,jsonb,boolean)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.lock_worker_lease_v1(
    p_worker_id text, p_instance_id text, p_task_id text, p_lease_token text
)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
BEGIN
    v_worker_id := sireader_workforce_private.worker_identity_v1(p_worker_id);
    SELECT t.* INTO v_task
      FROM public.workforce_tasks AS t
     WHERE t.task_id = p_task_id AND t.lease_token = p_lease_token
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Worker task lease was not found';
    END IF;
    SELECT a.* INTO v_attempt
      FROM public.workforce_task_attempts AS a
     WHERE a.task_id = v_task.task_id AND a.lease_token = v_task.lease_token
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Worker task attempt was not found';
    END IF;
    IF v_task.assigned_worker_id IS DISTINCT FROM v_worker_id
       OR v_attempt.worker_id IS DISTINCT FROM v_worker_id
       OR v_attempt.task_id IS DISTINCT FROM v_task.task_id
       OR v_attempt.lease_token IS DISTINCT FROM v_task.lease_token
       OR v_task.lease_owner IS DISTINCT FROM p_instance_id
       OR v_task.status NOT IN ('CLAIMED','RUNNING')
       OR v_task.lease_expires_at IS NULL
       OR v_task.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION 'Task lease is expired, mismatched, or owned by another worker instance';
    END IF;
    RETURN;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.lock_worker_lease_v1(text,text,text,text)
    OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.lock_worker_lease_v1(text,text,text,text)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_get_contract_v1(p_worker_id text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
    v_result jsonb;
BEGIN
    v_worker_id := sireader_workforce_private.worker_identity_v1(p_worker_id);
    SELECT pg_catalog.jsonb_build_object(
        'worker_id',w.worker_id,'display_name',w.display_name,'role',w.role,
        'department',w.department,'enabled',w.enabled,
        'max_concurrency',w.max_concurrency,'model_tier',w.model_tier,'schema_version',w.schema_version,
        'capability_ids',COALESCE((
            SELECT pg_catalog.jsonb_agg(wc.capability_id ORDER BY wc.capability_id)
            FROM public.workforce_worker_capabilities wc
            JOIN public.workforce_capabilities c ON c.capability_id = wc.capability_id
            WHERE wc.worker_id = w.worker_id AND wc.enabled AND c.enabled
        ),'[]'::jsonb)
    )
    INTO v_result
    FROM public.workforce_workers w
    WHERE w.worker_id = v_worker_id;
    IF v_result IS NULL OR (v_result->>'enabled')::boolean IS NOT TRUE THEN
        RAISE EXCEPTION 'Worker is not registered or disabled';
    END IF;
    RETURN v_result;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_get_contract_v1(text) OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_heartbeat_v1(
    p_worker_id text, p_instance_id text, p_process_state text,
    p_process_started_at timestamptz, p_heartbeat_at timestamptz,
    p_active_task_count integer, p_in_flight_provider_requests integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
    v_row public.workforce_heartbeats%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    v_worker_id := sireader_workforce_private.worker_identity_v1(p_worker_id);
    IF p_instance_id IS NULL OR pg_catalog.length(p_instance_id) NOT BETWEEN 8 AND 100
       OR p_process_state NOT IN ('STARTING','READY','DRAINING','STOPPED','FAILED')
       OR p_process_started_at IS NULL OR p_heartbeat_at IS NULL
       OR p_active_task_count NOT BETWEEN 0 AND 1000
       OR p_in_flight_provider_requests NOT BETWEEN 0 AND 1000
       OR p_heartbeat_at > v_now + interval '2 minutes'
       OR p_process_started_at > p_heartbeat_at THEN
        RAISE EXCEPTION 'Worker heartbeat input is invalid or outside permitted bounds';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_workers w
        WHERE w.worker_id = v_worker_id AND w.enabled
    ) THEN
        RAISE EXCEPTION 'Worker is not enabled';
    END IF;

    INSERT INTO public.workforce_heartbeats(
        heartbeat_id,worker_id,instance_id,process_state,process_started_at,
        heartbeat_at,active_task_count,in_flight_provider_requests
    ) VALUES (
        pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-',''),
        v_worker_id,p_instance_id,p_process_state,p_process_started_at,
        p_heartbeat_at,p_active_task_count,p_in_flight_provider_requests
    )
    ON CONFLICT (worker_id,instance_id) DO UPDATE
       SET process_state = EXCLUDED.process_state,
           process_started_at = EXCLUDED.process_started_at,
           heartbeat_at = EXCLUDED.heartbeat_at,
           active_task_count = EXCLUDED.active_task_count,
           in_flight_provider_requests = EXCLUDED.in_flight_provider_requests
     WHERE public.workforce_heartbeats.heartbeat_at <= EXCLUDED.heartbeat_at
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Worker heartbeat timestamp cannot move backwards';
    END IF;

    RETURN pg_catalog.to_jsonb(v_row);
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_heartbeat_v1(text,text,text,timestamptz,timestamptz,integer,integer)
    OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_claim_next_v1(
    p_worker_id text, p_instance_id text, p_lease_seconds integer, p_heartbeat_timeout_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
    v_worker public.workforce_workers%ROWTYPE;
    v_heartbeat public.workforce_heartbeats%ROWTYPE;
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_active_count integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_token text;
BEGIN
    v_worker_id := sireader_workforce_private.worker_identity_v1(p_worker_id);
    IF p_instance_id IS NULL OR pg_catalog.length(p_instance_id) NOT BETWEEN 8 AND 100
       OR p_lease_seconds NOT BETWEEN 5 AND 600
       OR p_heartbeat_timeout_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'Worker claim input is invalid or outside permitted bounds';
    END IF;

    SELECT * INTO v_worker
    FROM public.workforce_workers w
    WHERE w.worker_id = v_worker_id
    FOR UPDATE;
    IF NOT FOUND OR NOT v_worker.enabled THEN
        RAISE EXCEPTION 'Worker is not registered or enabled';
    END IF;
    IF NOT v_worker.accepting_tasks THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_heartbeat
    FROM public.workforce_heartbeats h
    WHERE h.worker_id = v_worker_id AND h.instance_id = p_instance_id
    FOR UPDATE;
    IF NOT FOUND OR v_heartbeat.process_state <> 'READY'
       OR v_heartbeat.heartbeat_at < v_now - pg_catalog.make_interval(secs => p_heartbeat_timeout_seconds) THEN
        RAISE EXCEPTION 'Worker process has no fresh READY heartbeat';
    END IF;

    SELECT pg_catalog.count(*)::integer INTO v_active_count
    FROM public.workforce_tasks t
    WHERE t.assigned_worker_id = v_worker_id
      AND t.status IN ('CLAIMED','RUNNING')
      AND t.lease_expires_at > v_now;
    IF v_active_count >= v_worker.max_concurrency THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_task
    FROM public.workforce_tasks t
    WHERE t.status IN ('QUEUED','RETRY_WAIT')
      AND t.attempts < t.max_attempts
      AND t.not_before <= v_now
      AND (t.deadline_at IS NULL OR t.deadline_at > v_now)
      AND (t.requested_worker_id IS NULL OR t.requested_worker_id = v_worker_id)
      AND EXISTS (
          SELECT 1
          FROM public.workforce_worker_capabilities wc
          JOIN public.workforce_capabilities c ON c.capability_id = wc.capability_id
          WHERE wc.worker_id = v_worker_id
            AND wc.enabled AND c.enabled
            AND c.capability_id = t.required_capability
            AND c.task_types ? t.task_type
      )
    ORDER BY least(
        100,
        t.priority + pg_catalog.floor(
            extract(epoch FROM (v_now - t.created_at)) / 300
        )::integer
    ) DESC, t.created_at ASC, t.task_id ASC
    LIMIT 1
    FOR UPDATE OF t SKIP LOCKED;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_token := pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-','');
    UPDATE public.workforce_tasks
       SET status = 'CLAIMED',
           assigned_worker_id = v_worker_id,
           attempts = attempts + 1,
           lease_token = v_token,
           lease_owner = p_instance_id,
           lease_expires_at = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
           updated_at = v_now
     WHERE task_id = v_task.task_id
    RETURNING * INTO v_task;

    INSERT INTO public.workforce_task_attempts(
        attempt_id,task_id,attempt_number,worker_id,lease_token,status,created_at
    ) VALUES (
        pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-',''),
        v_task.task_id,v_task.attempts,v_worker_id,v_token,'CLAIMED',v_now
    )
    RETURNING * INTO v_attempt;

    PERFORM sireader_workforce_private.append_worker_event_v1(
        'TASK_CLAIMED',v_worker_id,v_task.task_id,v_attempt.attempt_id,
        v_task.correlation_id,pg_catalog.jsonb_build_object('attempt_number',v_attempt.attempt_number),false
    );

    RETURN pg_catalog.jsonb_build_object(
        'task',pg_catalog.jsonb_build_object(
            'task_id',v_task.task_id,'task_type',v_task.task_type,
            'required_capability',v_task.required_capability,'title',v_task.title,
            'input_payload',v_task.input_payload,'correlation_id',v_task.correlation_id,
            'status',v_task.status,'attempts',v_task.attempts
        ),
        'attempt',pg_catalog.jsonb_build_object(
            'attempt_id',v_attempt.attempt_id,'task_id',v_attempt.task_id,
            'attempt_number',v_attempt.attempt_number,'worker_id',v_attempt.worker_id,
            'lease_token',v_attempt.lease_token,'status',v_attempt.status
        )
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_claim_next_v1(text,text,integer,integer)
    OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_start_v1(
    p_worker_id text,p_instance_id text,p_task_id text,p_lease_token text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    PERFORM sireader_workforce_private.lock_worker_lease_v1(p_worker_id,p_instance_id,p_task_id,p_lease_token);
    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=p_task_id AND lease_token=p_lease_token;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts WHERE task_id=p_task_id AND lease_token=p_lease_token;
    IF v_task.status <> 'CLAIMED' OR v_attempt.status <> 'CLAIMED' THEN
        RAISE EXCEPTION 'Only a claimed task/attempt can be started';
    END IF;

    UPDATE public.workforce_task_attempts
       SET status='STARTED',started_at=v_now
     WHERE attempt_id=v_attempt.attempt_id;
    UPDATE public.workforce_tasks
       SET status='RUNNING',updated_at=v_now
     WHERE task_id=v_task.task_id
    RETURNING * INTO v_task;

    PERFORM sireader_workforce_private.append_worker_event_v1(
        'TASK_STARTED',v_attempt.worker_id,v_task.task_id,v_attempt.attempt_id,
        v_task.correlation_id,'{}'::jsonb,false
    );
    RETURN pg_catalog.to_jsonb(v_task);
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_start_v1(text,text,text,text)
    OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_renew_lease_v1(
    p_worker_id text,p_instance_id text,p_task_id text,p_lease_token text,p_lease_seconds integer
)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_expiry timestamptz;
BEGIN
    IF p_lease_seconds NOT BETWEEN 5 AND 600 THEN
        RAISE EXCEPTION 'Lease renewal must be between 5 and 600 seconds';
    END IF;
    PERFORM sireader_workforce_private.lock_worker_lease_v1(p_worker_id,p_instance_id,p_task_id,p_lease_token);
    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=p_task_id AND lease_token=p_lease_token;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts WHERE task_id=p_task_id AND lease_token=p_lease_token;
    IF v_attempt.status NOT IN ('CLAIMED','STARTED','PROVIDER_REQUESTED') THEN
        RAISE EXCEPTION 'Attempt state does not permit lease renewal';
    END IF;
    v_expiry := pg_catalog.clock_timestamp() + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE public.workforce_tasks
       SET lease_expires_at=v_expiry,updated_at=pg_catalog.clock_timestamp()
     WHERE task_id=v_task.task_id;
    RETURN v_expiry;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_renew_lease_v1(text,text,text,text,integer)
    OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_reserve_provider_slot_v1(
    p_worker_id text,p_instance_id text,p_task_id text,p_lease_token text,p_provider_request_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_active bigint;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    PERFORM sireader_workforce_private.lock_worker_lease_v1(p_worker_id,p_instance_id,p_task_id,p_lease_token);
    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=p_task_id AND lease_token=p_lease_token;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts WHERE task_id=p_task_id AND lease_token=p_lease_token;
    IF v_attempt.worker_id NOT IN ('haiku','sonnet','opus') THEN
        RAISE EXCEPTION 'This worker identity is not permitted to reserve a model provider slot';
    END IF;
    IF v_task.required_capability IN (
        'rule_validation','decision_evaluation','risk_assessment_simulation',
        'decision_consistency_checks','market_monitoring_simulation',
        'market_condition_detection','anomaly_detection_simulation','market_event_reporting'
    ) THEN
        RAISE EXCEPTION 'Trading-related capabilities are not eligible for AI provider dispatch';
    END IF;
    IF v_task.status <> 'RUNNING' OR v_attempt.status <> 'STARTED' THEN
        RAISE EXCEPTION 'Provider reservation requires a RUNNING task and STARTED attempt';
    END IF;
    IF p_provider_request_id IS NULL OR pg_catalog.length(p_provider_request_id) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'Provider request identifier is invalid';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(61743092815670031::bigint);
    SELECT pg_catalog.count(*) INTO v_active
    FROM public.workforce_task_attempts a
    WHERE a.status IN ('PROVIDER_REQUESTED','UNKNOWN');

    IF v_active >= 1 THEN
        RETURN pg_catalog.jsonb_build_object('acquired',false,'attempt_id',v_attempt.attempt_id);
    END IF;

    UPDATE public.workforce_task_attempts
       SET status='PROVIDER_REQUESTED',
           provider_request_id=p_provider_request_id,
           provider_started_at=v_now
     WHERE attempt_id=v_attempt.attempt_id;
    PERFORM sireader_workforce_private.append_worker_event_v1(
        'PROVIDER_REQUESTED',v_attempt.worker_id,v_task.task_id,v_attempt.attempt_id,
        v_task.correlation_id,pg_catalog.jsonb_build_object('provider_request_id',p_provider_request_id),false
    );
    RETURN pg_catalog.jsonb_build_object(
        'acquired',true,'attempt_id',v_attempt.attempt_id,
        'provider_request_id',p_provider_request_id,'status','PROVIDER_REQUESTED'
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)
    OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_complete_task_v1(
    p_worker_id text,p_instance_id text,p_task_id text,p_lease_token text,
    p_result_payload jsonb,p_billing_currency text,p_cost_amount numeric,p_policy_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_requires_review boolean;
    v_review_id text;
    v_review_payload jsonb;
    v_hash text;
    v_source text;
    v_expected_tier text;
    v_context jsonb := COALESCE(p_policy_context,'{}'::jsonb);
BEGIN
    PERFORM sireader_workforce_private.lock_worker_lease_v1(p_worker_id,p_instance_id,p_task_id,p_lease_token);
    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=p_task_id AND lease_token=p_lease_token;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts WHERE task_id=p_task_id AND lease_token=p_lease_token;
    IF v_task.status <> 'RUNNING' OR v_attempt.status NOT IN ('STARTED','PROVIDER_REQUESTED') THEN
        RAISE EXCEPTION 'Only a running task with a current started/provider attempt can complete';
    END IF;
    IF p_result_payload IS NULL OR pg_catalog.jsonb_typeof(p_result_payload) <> 'object'
       OR pg_catalog.octet_length(sireader_workforce_private.canonical_json_v1(p_result_payload)) > 65536 THEN
        RAISE EXCEPTION 'Task result payload must be a JSON object no larger than 64 KiB';
    END IF;

    -- Persisted provenance is database-validated, not caller-authored telemetry.
    v_source := p_result_payload->>'source';
    IF v_source IS NULL OR v_source NOT IN ('LIVE','PYTHON','SIMULATED') THEN
        RAISE EXCEPTION 'Task result source must be an explicit supported provenance value';
    END IF;
    IF v_attempt.status = 'PROVIDER_REQUESTED' THEN
        IF v_source <> 'LIVE'
           OR v_attempt.provider_request_id IS NULL
           OR p_billing_currency IS DISTINCT FROM 'USD'
           OR p_cost_amount IS NULL
           OR v_attempt.worker_id NOT IN ('haiku','sonnet','opus') THEN
            RAISE EXCEPTION 'LIVE result provenance requires a reserved AI request and USD billing';
        END IF;
    ELSE
        IF v_source = 'LIVE'
           OR p_billing_currency IS NOT NULL
           OR p_cost_amount IS NOT NULL THEN
            RAISE EXCEPTION 'Unreserved task completion cannot claim LIVE provenance or provider billing';
        END IF;
    END IF;
    IF v_source = 'PYTHON'
       AND v_attempt.worker_id NOT IN ('supervisor','decision_engineer','market_monitor','data_archivist') THEN
        RAISE EXCEPTION 'PYTHON provenance is restricted to deterministic Python worker profiles';
    END IF;
    IF v_source = 'LIVE'
       AND v_attempt.worker_id NOT IN ('haiku','sonnet','opus') THEN
        RAISE EXCEPTION 'LIVE provenance is restricted to approved AI worker profiles';
    END IF;

    IF p_billing_currency IS NOT NULL AND p_billing_currency NOT IN ('IDR','USD') THEN
        RAISE EXCEPTION 'Billing currency must be IDR or USD';
    END IF;
    IF p_cost_amount IS NOT NULL AND (p_cost_amount < 0 OR p_cost_amount >= 1000000000000) THEN
        RAISE EXCEPTION 'Cost amount is outside the permitted range';
    END IF;
    IF pg_catalog.jsonb_typeof(v_context) <> 'object'
       OR pg_catalog.octet_length(sireader_workforce_private.canonical_json_v1(v_context)) > 16384
       OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_context) AS keys(key)
                  WHERE key NOT IN ('policy_version','sensitive_action','model_tier','reason_code','redacted_fields')) THEN
        RAISE EXCEPTION 'Policy context has an invalid shape or exceeds 16 KiB';
    END IF;
    IF v_context->>'policy_version' IS DISTINCT FROM '2026.11.10-harness.1'
       OR v_context->>'sensitive_action' IS NULL
       OR v_context->>'sensitive_action' NOT IN ('ALLOW','REDACT','REVIEW')
       OR v_context->>'reason_code' IS NULL
       OR v_context->>'reason_code' NOT IN (
           'POLICY_ALLOWED','CREDENTIALS_REDACTED','SENSITIVE_PERSONAL_DATA_REVIEW'
       )
       OR v_context->>'model_tier' IS NULL THEN
        RAISE EXCEPTION 'Worker policy provenance is missing, unknown, or unsupported';
    END IF;
    v_expected_tier := CASE v_attempt.worker_id
        WHEN 'haiku' THEN 'FAST'
        WHEN 'sonnet' THEN 'REASONING'
        WHEN 'opus' THEN 'ESCALATION'
        ELSE 'NONE'
    END;
    IF v_context->>'model_tier' IS DISTINCT FROM v_expected_tier THEN
        RAISE EXCEPTION 'Worker policy model tier does not match its registered profile';
    END IF;
    IF v_context ? 'redacted_fields'
       AND (v_context->>'redacted_fields') !~ '^[0-9]{1,9}$' THEN
        RAISE EXCEPTION 'Policy context redacted_fields must be a non-negative integer';
    END IF;
    IF (v_context->>'sensitive_action' = 'ALLOW'
        AND (v_context->>'reason_code' <> 'POLICY_ALLOWED'
             OR COALESCE((v_context->>'redacted_fields')::integer,0) <> 0))
       OR (v_context->>'sensitive_action' = 'REDACT'
        AND v_context->>'reason_code' <> 'CREDENTIALS_REDACTED')
       OR (v_context->>'sensitive_action' = 'REVIEW'
        AND (v_context->>'reason_code' <> 'SENSITIVE_PERSONAL_DATA_REVIEW'
             OR COALESCE((v_context->>'redacted_fields')::integer,0) < 1)) THEN
        RAISE EXCEPTION 'Worker policy action, reason, and redaction count are inconsistent';
    END IF;

    v_requires_review := sireader_workforce_private.requires_review_v1(
        v_task.required_capability,v_task.task_type,v_task.task_id,v_task.title,v_task.input_payload
    ) OR sireader_workforce_private.contains_personal_data_v1(p_result_payload)
      OR (v_source = 'LIVE' AND COALESCE((p_result_payload->>'output_truncated')::boolean,false));
    IF v_requires_review THEN
        v_review_payload := sireader_workforce_private.sanitize_review_payload_v1(p_result_payload);
        IF pg_catalog.octet_length(sireader_workforce_private.canonical_json_v1(v_review_payload)) > 65536 THEN
            RAISE EXCEPTION 'Sanitized review payload exceeds 64 KiB';
        END IF;
        v_hash := pg_catalog.encode(
            extensions.digest(
                pg_catalog.convert_to(sireader_workforce_private.canonical_json_v1(v_review_payload),'UTF8'),
                'sha256'
            ),
            'hex'
        );
        v_review_id := 'rev_' || pg_catalog.substr(pg_catalog.replace(pg_catalog.gen_random_uuid()::text,'-',''),1,16);
    END IF;

    UPDATE public.workforce_task_attempts
       SET status='COMPLETED',finished_at=v_now,
           billing_currency=p_billing_currency,cost_amount=p_cost_amount
     WHERE attempt_id=v_attempt.attempt_id;

    IF v_requires_review THEN
        INSERT INTO public.workforce_task_reviews(
            review_id,task_id,attempt_id,status,original_output_sha256,review_expires_at,created_at,updated_at
        ) VALUES (
            v_review_id,v_task.task_id,v_attempt.attempt_id,'PENDING',v_hash,
            v_now + interval '24 hours',v_now,v_now
        );
        INSERT INTO public.workforce_task_review_payloads(
            review_id,original_output,created_at,updated_at
        ) VALUES (v_review_id,v_review_payload,v_now,v_now);
        PERFORM sireader_workforce_private.append_worker_event_v1(
            'HUMAN_REVIEW_QUEUED',v_attempt.worker_id,v_task.task_id,v_attempt.attempt_id,
            v_task.correlation_id,
            pg_catalog.jsonb_build_object(
                'review_id',v_review_id,'original_output_sha256',v_hash,
                'trigger_reason',CASE
                    WHEN v_source = 'LIVE'
                         AND COALESCE((p_result_payload->>'output_truncated')::boolean,false)
                    THEN 'OUTPUT_TRUNCATED_REVIEW'
                    WHEN sireader_workforce_private.contains_personal_data_v1(v_task.input_payload)
                         OR v_task.title ~* '\m[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\M'
                    THEN 'SENSITIVE_PERSONAL_DATA_REVIEW'
                    ELSE 'CAPABILITY_REVIEW_POLICY'
                END
            ),true
        );
    END IF;

    UPDATE public.workforce_tasks
       SET status='COMPLETED',
           result_payload=CASE WHEN v_requires_review THEN NULL ELSE p_result_payload END,
           completed_at=v_now,
           payload_expires_at=v_now + interval '30 days',
           updated_at=v_now,
           lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
     WHERE task_id=v_task.task_id
    RETURNING * INTO v_task;

    PERFORM sireader_workforce_private.append_worker_event_v1(
        'TASK_COMPLETED',v_attempt.worker_id,v_task.task_id,v_attempt.attempt_id,
        v_task.correlation_id,
        pg_catalog.jsonb_build_object(
            'source',v_source,
            'requires_review',v_requires_review,
            'policy_version',COALESCE(v_context->>'policy_version','2026.11.10-harness.1'),
            'sensitive_action',COALESCE(v_context->>'sensitive_action','ALLOW'),
            'model_tier',COALESCE(v_context->>'model_tier','NONE'),
            'reason_code',COALESCE(v_context->>'reason_code','POLICY_ALLOWED'),
            'redacted_fields',COALESCE((v_context->>'redacted_fields')::integer,0)
        ),false
    );

    RETURN pg_catalog.jsonb_build_object(
        'task_id',v_task.task_id,'status',v_task.status,
        'completed_at',v_task.completed_at,'requires_review',v_requires_review,
        'review_id',v_review_id
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_complete_task_v1(text,text,text,text,jsonb,text,numeric,jsonb)
    OWNER TO sireader_workforce_fn_owner;

CREATE OR REPLACE FUNCTION sireader_workforce_private.worker_fail_task_v1(
    p_worker_id text,p_instance_id text,p_task_id text,p_lease_token text,
    p_error_code text,p_retryable boolean,p_outcome_unknown boolean,p_backoff_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_task public.workforce_tasks%ROWTYPE;
    v_attempt public.workforce_task_attempts%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_provider_requested boolean;
    v_next_status text;
    v_attempt_status text;
    v_event_type text;
    v_expiry timestamptz;
    v_error_code text := pg_catalog.left(COALESCE(NULLIF(pg_catalog.btrim(p_error_code),''),'WORKER_FAILED'),80);
BEGIN
    PERFORM sireader_workforce_private.lock_worker_lease_v1(p_worker_id,p_instance_id,p_task_id,p_lease_token);
    SELECT * INTO v_task FROM public.workforce_tasks WHERE task_id=p_task_id AND lease_token=p_lease_token;
    SELECT * INTO v_attempt FROM public.workforce_task_attempts WHERE task_id=p_task_id AND lease_token=p_lease_token;
    IF p_retryable IS NULL OR p_outcome_unknown IS NULL
       OR p_backoff_seconds IS NULL OR p_backoff_seconds < 1 OR p_backoff_seconds > 3600 THEN
        RAISE EXCEPTION 'Worker failure policy parameters are invalid';
    END IF;

    v_provider_requested := v_attempt.provider_request_id IS NOT NULL OR v_attempt.status='PROVIDER_REQUESTED';
    IF p_outcome_unknown OR v_provider_requested THEN
        v_next_status := 'WAITING_REVIEW';
        v_attempt_status := 'UNKNOWN';
        v_event_type := 'TASK_WAITING_REVIEW';
        v_expiry := v_now + interval '90 days';
    ELSIF p_retryable AND v_task.attempts < v_task.max_attempts THEN
        v_next_status := 'RETRY_WAIT';
        v_attempt_status := 'FAILED';
        v_event_type := 'TASK_RETRIED';
        v_expiry := NULL;
    ELSIF v_task.attempts >= v_task.max_attempts THEN
        v_next_status := 'DEAD_LETTERED';
        v_attempt_status := 'FAILED';
        v_event_type := 'TASK_DEAD_LETTERED';
        v_expiry := v_now + interval '30 days';
    ELSE
        v_next_status := 'FAILED';
        v_attempt_status := 'FAILED';
        v_event_type := 'TASK_FAILED';
        v_expiry := v_now + interval '30 days';
    END IF;

    UPDATE public.workforce_task_attempts
       SET status=v_attempt_status,error_code=v_error_code,finished_at=v_now
     WHERE attempt_id=v_attempt.attempt_id;
    UPDATE public.workforce_tasks
       SET status=v_next_status,failure_code=v_error_code,
           not_before=CASE WHEN v_next_status='RETRY_WAIT'
                           THEN v_now + pg_catalog.make_interval(secs => least(3600,greatest(1,p_backoff_seconds)))
                           ELSE not_before END,
           payload_expires_at=v_expiry,updated_at=v_now,
           lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL
     WHERE task_id=v_task.task_id
    RETURNING * INTO v_task;

    PERFORM sireader_workforce_private.append_worker_event_v1(
        v_event_type,v_attempt.worker_id,v_task.task_id,v_attempt.attempt_id,
        v_task.correlation_id,
        pg_catalog.jsonb_build_object(
            'error_code',v_error_code,
            'retryable',p_retryable,
            'outcome_unknown',(p_outcome_unknown OR v_provider_requested)
        ),false
    );
    RETURN pg_catalog.jsonb_build_object(
        'task_id',v_task.task_id,'status',v_task.status,'failure_code',v_task.failure_code,
        'payload_expires_at',v_task.payload_expires_at
    );
END;
$function$;
ALTER FUNCTION sireader_workforce_private.worker_fail_task_v1(text,text,text,text,text,boolean,boolean,integer)
    OWNER TO sireader_workforce_fn_owner;

-- Result publication must be tied to the immediately previous active lease and its exact attempt.
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
BEGIN
    IF NEW.result_payload IS NOT DISTINCT FROM OLD.result_payload THEN
        RETURN NEW;
    END IF;

    IF NEW.result_payload IS NULL THEN
        IF OLD.result_payload IS NOT NULL
           AND NEW.payload_purged_at IS NOT NULL
           AND NEW.payload_expires_at IS NOT NULL
           AND NEW.payload_expires_at <= pg_catalog.clock_timestamp()
           AND pg_catalog.split_part(session_user::text, '.', 1)
               IN ('sireader_workforce_maint','postgres') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Task result may only be cleared by an authorized retention boundary';
    END IF;

    IF OLD.result_payload IS NOT NULL THEN
        RAISE EXCEPTION 'Task result publication is write-once';
    END IF;
    IF NEW.status <> 'COMPLETED' THEN
        RAISE EXCEPTION 'Task result may only be published for a completed task';
    END IF;

    SELECT p.worker_id INTO v_worker_id
    FROM sireader_workforce_private.worker_principals p
    WHERE p.database_role = pg_catalog.split_part(session_user::text, '.', 1)::name AND p.enabled;

    IF v_worker_id IS NOT NULL THEN
        IF OLD.status <> 'RUNNING'
           OR OLD.lease_token IS NULL
           OR OLD.lease_expires_at IS NULL
           OR OLD.lease_expires_at <= pg_catalog.clock_timestamp()
           OR OLD.assigned_worker_id IS DISTINCT FROM v_worker_id
           OR NEW.lease_token IS NOT NULL
           OR NEW.lease_owner IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR EXISTS (SELECT 1 FROM public.workforce_task_reviews r WHERE r.task_id=OLD.task_id)
           OR NOT EXISTS (
               SELECT 1 FROM public.workforce_task_attempts a
               WHERE a.task_id=OLD.task_id
                 AND a.lease_token=OLD.lease_token
                 AND a.worker_id=v_worker_id
                 AND a.status='COMPLETED'
                 AND a.finished_at=NEW.completed_at
           ) THEN
            RAISE EXCEPTION 'Worker result is not bound to the previous active lease and matching completed attempt';
        END IF;
        RETURN NEW;
    END IF;

    IF session_user = 'sireader_workforce_cmd' THEN
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

    RAISE EXCEPTION 'Task result release requires an authenticated worker completion or approved human review';
END;
$function$;
ALTER FUNCTION sireader_workforce_private.guard_worker_result_release_v1() OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.guard_worker_result_release_v1() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_sireader_worker_result_release ON public.workforce_tasks;
CREATE TRIGGER trg_sireader_worker_result_release
BEFORE UPDATE OF result_payload ON public.workforce_tasks
FOR EACH ROW EXECUTE FUNCTION sireader_workforce_private.guard_worker_result_release_v1();

CREATE OR REPLACE FUNCTION sireader_workforce_private.guard_worker_event_actor_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
BEGIN
    SELECT p.worker_id INTO v_worker_id
    FROM sireader_workforce_private.worker_principals p
    WHERE p.database_role = pg_catalog.split_part(session_user::text, '.', 1)::name AND p.enabled;

    IF v_worker_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.actor_type='WORKER'
       AND NEW.actor_id=v_worker_id
       AND NEW.worker_id=v_worker_id
       AND NEW.event_type IN (
           'TASK_CLAIMED','TASK_STARTED','PROVIDER_REQUESTED','TASK_COMPLETED',
           'TASK_FAILED','TASK_RETRIED','TASK_WAITING_REVIEW','TASK_DEAD_LETTERED'
       ) THEN
        RETURN NEW;
    END IF;

    IF NEW.actor_type='SYSTEM'
       AND NEW.actor_id='system'
       AND NEW.event_type='HUMAN_REVIEW_QUEUED'
       AND NEW.worker_id=v_worker_id
       AND EXISTS (
           SELECT 1 FROM public.workforce_task_reviews r
           WHERE r.review_id = NEW.payload->>'review_id'
             AND r.task_id=NEW.task_id
             AND r.attempt_id=NEW.attempt_id
             AND r.status='PENDING'
       ) THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'Workforce event actor/type is not permitted for this authenticated worker identity';
END;
$function$;
ALTER FUNCTION sireader_workforce_private.guard_worker_event_actor_v1() OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.guard_worker_event_actor_v1() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_sireader_worker_event_actor ON public.workforce_activity_events;
CREATE TRIGGER trg_sireader_worker_event_actor
BEFORE INSERT ON public.workforce_activity_events
FOR EACH ROW EXECUTE FUNCTION sireader_workforce_private.guard_worker_event_actor_v1();

CREATE OR REPLACE FUNCTION sireader_workforce_private.guard_worker_review_provenance_v1()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_worker_id text;
BEGIN
    SELECT p.worker_id INTO v_worker_id
    FROM sireader_workforce_private.worker_principals p
    WHERE p.database_role = pg_catalog.split_part(session_user::text, '.', 1)::name AND p.enabled;

    IF v_worker_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.status <> 'PENDING'
       OR NEW.adjusted_output_sha256 IS NOT NULL
       OR NEW.adjudicated_by IS NOT NULL
       OR NEW.adjudicated_at IS NOT NULL
       OR NEW.reason_code IS NOT NULL
       OR NEW.notes IS NOT NULL
       OR NOT EXISTS (
           SELECT 1
           FROM public.workforce_tasks t
           JOIN public.workforce_task_attempts a
             ON a.task_id=t.task_id
            AND a.attempt_id=NEW.attempt_id
            AND a.lease_token=t.lease_token
           WHERE t.task_id=NEW.task_id
             AND t.status='RUNNING'
             AND t.assigned_worker_id=v_worker_id
             AND a.worker_id=v_worker_id
             AND a.status='COMPLETED'
             AND a.finished_at=NEW.created_at
             AND t.lease_expires_at > pg_catalog.clock_timestamp()
       ) THEN
        RAISE EXCEPTION 'Review creation must be bound to the exact active worker lease and completing attempt';
    END IF;
    RETURN NEW;
END;
$function$;
ALTER FUNCTION sireader_workforce_private.guard_worker_review_provenance_v1() OWNER TO sireader_workforce_fn_owner;
REVOKE ALL ON FUNCTION sireader_workforce_private.guard_worker_review_provenance_v1() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_sireader_worker_review_provenance ON public.workforce_task_reviews;
CREATE TRIGGER trg_sireader_worker_review_provenance
BEFORE INSERT ON public.workforce_task_reviews
FOR EACH ROW EXECUTE FUNCTION sireader_workforce_private.guard_worker_review_provenance_v1();

-- Each worker can execute only its worker-specific command surface. No base-table grants.
DO $grants$
DECLARE
    v_role text;
    v_suffix text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
        'sireader_worker_opus','sireader_worker_decision_engineer',
        'sireader_worker_market_monitor','sireader_worker_data_archivist'
    ] LOOP
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_get_contract_v1(text) TO %I',v_role);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_heartbeat_v1(text,text,text,timestamptz,timestamptz,integer,integer) TO %I',v_role);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_claim_next_v1(text,text,integer,integer) TO %I',v_role);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_start_v1(text,text,text,text) TO %I',v_role);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_renew_lease_v1(text,text,text,text,integer) TO %I',v_role);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_complete_task_v1(text,text,text,text,jsonb,text,numeric,jsonb) TO %I',v_role);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_fail_task_v1(text,text,text,text,text,boolean,boolean,integer) TO %I',v_role);
    END LOOP;
    FOREACH v_role IN ARRAY ARRAY['sireader_worker_haiku','sireader_worker_sonnet','sireader_worker_opus'] LOOP
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text) TO %I',v_role);
    END LOOP;
END;
$grants$;

REVOKE ALL ON FUNCTION sireader_workforce_private.worker_get_contract_v1(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_heartbeat_v1(text,text,text,timestamptz,timestamptz,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_claim_next_v1(text,text,integer,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_start_v1(text,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_renew_lease_v1(text,text,text,text,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_complete_task_v1(text,text,text,text,jsonb,text,numeric,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION sireader_workforce_private.worker_fail_task_v1(text,text,text,text,text,boolean,boolean,integer) FROM PUBLIC;

-- Function ownership and effective privilege audit.
DO $audit$
DECLARE
    v_name text;
    v_fn record;
BEGIN
    FOR v_fn IN
        SELECT p.oid,p.proname,pg_catalog.pg_get_function_identity_arguments(p.oid) AS args
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='sireader_workforce_private'
          AND p.proname LIKE 'worker_%'
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION sireader_workforce_private.%I(%s) OWNER TO sireader_workforce_fn_owner',v_fn.proname,v_fn.args);
        IF NOT (SELECT prosecdef FROM pg_catalog.pg_proc WHERE oid=v_fn.oid) THEN
            RAISE EXCEPTION 'Public worker command % must be SECURITY DEFINER',v_fn.proname;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members m
        JOIN pg_catalog.pg_roles r ON r.oid=m.member
        WHERE r.rolname IN (
            'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
            'sireader_worker_opus','sireader_worker_decision_engineer',
            'sireader_worker_market_monitor','sireader_worker_data_archivist'
        )
    ) THEN
        RAISE EXCEPTION 'A worker role inherits another role';
    END IF;
END;
$audit$;

-- Keep the temporary migration-operator membership and schema CREATE grant until
-- the immediately following operator/review/maintenance staging layer has transferred
-- its additional function/table ownership and granted its narrow EXECUTE surfaces.
COMMIT;

-- IMPORTANT: this staging layer replaces neither command API nor maintenance.
-- Runtime adapter conversion, independent review, and the dedicated maintenance
-- function surface remain separate acceptance gates before any production use.
