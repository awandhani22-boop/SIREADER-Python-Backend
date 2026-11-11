-- SIREADER workforce authority production migration v1.
-- Creates LOGIN roles with NO password; provision/rotate secrets through a separate approved secret process.
-- This migration is intended for the hosted target only after the managed-staging rehearsal and release gate pass.
BEGIN;

-- Phase 7.4 worker DB command authority layer.
-- Creates seven dedicated worker LOGIN identities without passwords.
-- Provision/rotate credentials through a separate approved secret-management process.

DO $guard$
DECLARE v_tables integer; v_views integer;
BEGIN
    IF current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Workforce production migration must be applied by the postgres migration operator';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_view_owner'
                   AND NOT rolcanlogin AND NOT rolsuper AND NOT rolinherit AND NOT rolbypassrls)
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_api_ro'
                      AND rolcanlogin AND NOT rolsuper AND NOT rolinherit AND NOT rolbypassrls) THEN
        RAISE EXCEPTION 'Phase 3.1 read-only roles are missing or unsafe';
    END IF;
    SELECT count(*) INTO v_tables FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname LIKE 'workforce_%' AND c.relkind IN ('r','p');
    SELECT count(*) INTO v_views FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname LIKE 'v_api_%' AND c.relkind='v'
      AND EXISTS (SELECT 1 FROM pg_catalog.pg_options_to_table(c.reloptions)
                  WHERE option_name='security_barrier' AND option_value='true');
    IF v_tables <> 9 OR v_views <> 14 THEN
        RAISE EXCEPTION 'Expected nine RLS workforce base tables and fourteen security-barrier API views; found %, %',v_tables,v_views;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname LIKE 'workforce_%'
          AND c.relkind IN ('r','p') AND NOT c.relrowsecurity
    ) THEN
        RAISE EXCEPTION 'Every workforce base table must have RLS enabled';
    END IF;
    IF pg_catalog.to_regnamespace('sireader_workforce_private') IS NOT NULL
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_workforce_fn_owner') THEN
        RAISE EXCEPTION 'A worker database command layer already exists; reconcile before migration';
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
                'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                v_role
            );
        ELSE
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = v_role
                  AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
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

DO $role_config$
DECLARE v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
        'sireader_worker_opus','sireader_worker_decision_engineer',
        'sireader_worker_market_monitor','sireader_worker_data_archivist'
    ] LOOP
        EXECUTE pg_catalog.format('ALTER ROLE %I SET search_path = pg_catalog',v_role);
    END LOOP;
END;
$role_config$;

-- Supabase's managed postgres role is CREATEROLE but not a superuser.
-- Grant it temporary SET membership so it can transfer internal objects to
-- the NOLOGIN function owner; the membership is revoked before COMMIT.
GRANT sireader_workforce_fn_owner TO postgres WITH SET TRUE, INHERIT FALSE;

-- Keep schema ownership with the migration operator. The NOLOGIN function owner
-- receives CREATE only while objects are being transferred to it.
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

-- Seed and revoke grants as the NOLOGIN owner, not as the migration operator.
SET ROLE sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.worker_principals FROM PUBLIC;

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

SET ROLE sireader_workforce_fn_owner;
-- Each worker receives only its worker-specific callable function surface.
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
RESET ROLE;


-- IMPORTANT: this staging layer replaces neither command API nor maintenance.
-- Runtime adapter conversion, independent review, and the dedicated maintenance
-- function surface remain separate acceptance gates before any production use.

-- SIREADER production migration continuation: command API, trusted review, and bounded maintenance authority.

DO $guard$
BEGIN
    IF current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Workforce authority continuation must be applied by the postgres migration operator';
    END IF;
    IF pg_catalog.to_regnamespace('sireader_workforce_private') IS NULL
       OR pg_catalog.to_regprocedure('sireader_workforce_private.worker_get_contract_v1(text)') IS NULL
       OR pg_catalog.to_regprocedure('sireader_workforce_private.worker_complete_task_v1(text,text,text,text,jsonb,text,numeric,jsonb)') IS NULL THEN
        RAISE EXCEPTION 'Worker command database layer is not installed';
    END IF;
    IF pg_catalog.to_regclass('sireader_workforce_private.authority_principals') IS NOT NULL
       OR pg_catalog.to_regprocedure('sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text)') IS NOT NULL THEN
        RAISE EXCEPTION 'Operator/review/maintenance authority already exists; reconcile before migration';
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
                'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                v_role
            );
        ELSE
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = v_role
                  AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolinherit)
            ) THEN
                RAISE EXCEPTION 'Authority role % violates the staging least-privilege contract', v_role;
            END IF;
        END IF;
    END LOOP;
END;
$roles$;

DO $role_config$
DECLARE v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'sireader_workforce_cmd','sireader_workforce_review','sireader_workforce_maint'
    ] LOOP
        EXECUTE pg_catalog.format('ALTER ROLE %I SET search_path = pg_catalog',v_role);
    END LOOP;
END;
$role_config$;

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
SET ROLE sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.authority_principals FROM PUBLIC;
INSERT INTO sireader_workforce_private.authority_principals(database_role,authority_type,actor_id)
VALUES
    ('sireader_workforce_cmd','COMMAND','local_operator'),
    ('sireader_workforce_review','REVIEW','local_operator'),
    ('sireader_workforce_maint','MAINTENANCE','system')
ON CONFLICT (database_role) DO UPDATE
SET authority_type=EXCLUDED.authority_type, actor_id=EXCLUDED.actor_id, enabled=true;
RESET ROLE;

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
SET ROLE sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.authority_command_audit FROM PUBLIC;
RESET ROLE;

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
SET ROLE sireader_workforce_fn_owner;
REVOKE ALL ON sireader_workforce_private.authority_maintenance_audit FROM PUBLIC;
RESET ROLE;

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

-- Replace the release trigger predicate as its existing NOLOGIN owner.
SET ROLE sireader_workforce_fn_owner;
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
RESET ROLE;

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
-- The function owner now owns these entry points/tables. Use its explicit SET
-- membership for the narrow grants, then return to the migration operator.
SET ROLE sireader_workforce_fn_owner;

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
-- New PostgreSQL functions grant EXECUTE to PUBLIC by default. Revoke this
-- once, as the NOLOGIN function owner, after every private function is owned.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA sireader_workforce_private FROM PUBLIC;

RESET ROLE;

DO $final_audit$
DECLARE v_expected_login_roles integer; v_public_exec integer; v_direct_grants integer;
BEGIN
    SELECT count(*) INTO v_expected_login_roles
    FROM pg_catalog.pg_roles
    WHERE rolname IN (
        'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
        'sireader_worker_opus','sireader_worker_decision_engineer',
        'sireader_worker_market_monitor','sireader_worker_data_archivist',
        'sireader_workforce_cmd','sireader_workforce_review','sireader_workforce_maint'
    )
      AND rolcanlogin AND NOT rolsuper AND NOT rolinherit AND NOT rolcreatedb
      AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls;
    IF v_expected_login_roles <> 10 THEN
        RAISE EXCEPTION 'Expected ten distinct hardened workforce LOGIN identities; found %',v_expected_login_roles;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles r
        WHERE r.rolname IN (
            'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
            'sireader_worker_opus','sireader_worker_decision_engineer',
            'sireader_worker_market_monitor','sireader_worker_data_archivist',
            'sireader_workforce_cmd','sireader_workforce_review','sireader_workforce_maint'
        )
        AND EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid)
    ) THEN RAISE EXCEPTION 'A workforce LOGIN identity inherits another database role'; END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname LIKE 'workforce_%'
          AND c.relkind IN ('r','p')
          AND NOT c.relrowsecurity
    ) THEN RAISE EXCEPTION 'RLS is not enabled on every workforce base table'; END IF;

    SELECT count(*) INTO v_direct_grants
    FROM pg_catalog.pg_roles r
    CROSS JOIN pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE r.rolname IN (
        'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
        'sireader_worker_opus','sireader_worker_decision_engineer',
        'sireader_worker_market_monitor','sireader_worker_data_archivist',
        'sireader_workforce_cmd','sireader_workforce_review','sireader_workforce_maint'
    )
      AND n.nspname='public' AND c.relname LIKE 'workforce_%'
      AND c.relkind IN ('r','p')
      AND (
        has_table_privilege(r.rolname,c.oid,'SELECT')
        OR has_table_privilege(r.rolname,c.oid,'INSERT')
        OR has_table_privilege(r.rolname,c.oid,'UPDATE')
        OR has_table_privilege(r.rolname,c.oid,'DELETE')
        OR has_table_privilege(r.rolname,c.oid,'TRUNCATE')
        OR has_table_privilege(r.rolname,c.oid,'REFERENCES')
        OR has_table_privilege(r.rolname,c.oid,'TRIGGER')
      );
    IF v_direct_grants <> 0 THEN
        RAISE EXCEPTION 'Workforce LOGIN roles have direct base-table privileges';
    END IF;

    SELECT count(*) INTO v_public_exec
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
    CROSS JOIN LATERAL pg_catalog.aclexplode(
        COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))
    ) acl
    WHERE n.nspname='sireader_workforce_private'
      AND acl.grantee=0 AND acl.privilege_type='EXECUTE';
    IF v_public_exec <> 0 THEN
        RAISE EXCEPTION 'PUBLIC has EXECUTE on % private workforce functions',v_public_exec;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles r
        WHERE r.rolname='sireader_workforce_fn_owner'
          AND (r.rolcanlogin OR r.rolsuper OR r.rolinherit OR r.rolcreatedb
               OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
    ) THEN RAISE EXCEPTION 'Function owner role violates NOLOGIN least privilege'; END IF;
END;
$final_audit$;

DO $function_acl_audit$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles r
        WHERE r.rolname IN (
            'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
            'sireader_worker_opus','sireader_worker_decision_engineer',
            'sireader_worker_market_monitor','sireader_worker_data_archivist'
        )
        AND (
            NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_get_contract_v1(text)','EXECUTE')
            OR NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_heartbeat_v1(text,text,text,timestamptz,timestamptz,integer,integer)','EXECUTE')
            OR NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_claim_next_v1(text,text,integer,integer)','EXECUTE')
            OR NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_start_v1(text,text,text,text)','EXECUTE')
            OR NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_renew_lease_v1(text,text,text,text,integer)','EXECUTE')
            OR NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_complete_task_v1(text,text,text,text,jsonb,text,numeric,jsonb)','EXECUTE')
            OR NOT pg_catalog.has_function_privilege(r.rolname,'sireader_workforce_private.worker_fail_task_v1(text,text,text,text,text,boolean,boolean,integer)','EXECUTE')
        )
    ) THEN
        RAISE EXCEPTION 'A worker login is missing one or more lifecycle EXECUTE grants';
    END IF;

    IF NOT pg_catalog.has_function_privilege('sireader_worker_haiku','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE')
       OR NOT pg_catalog.has_function_privilege('sireader_worker_sonnet','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE')
       OR NOT pg_catalog.has_function_privilege('sireader_worker_opus','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_worker_supervisor','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_worker_decision_engineer','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_worker_market_monitor','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_worker_data_archivist','sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)','EXECUTE') THEN
        RAISE EXCEPTION 'Provider reservation function grants do not match AI worker allowlist';
    END IF;

    IF NOT pg_catalog.has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text)','EXECUTE')
       OR NOT pg_catalog.has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.command_cancel_task_v1(text,text)','EXECUTE')
       OR NOT pg_catalog.has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.command_retry_task_v1(text,text)','EXECUTE')
       OR NOT pg_catalog.has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.command_set_worker_scheduling_v1(text,boolean,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.review_get_pending_v1(text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)','EXECUTE') THEN
        RAISE EXCEPTION 'Command API function grants are not the exact allowlist';
    END IF;

    IF NOT pg_catalog.has_function_privilege('sireader_workforce_review','sireader_workforce_private.review_get_pending_v1(text)','EXECUTE')
       OR NOT pg_catalog.has_function_privilege('sireader_workforce_review','sireader_workforce_private.review_adjudicate_v1(text,text,text,jsonb,text,text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_workforce_review','sireader_workforce_private.command_cancel_task_v1(text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_workforce_review','sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)','EXECUTE') THEN
        RAISE EXCEPTION 'Trusted review function grants are not the exact allowlist';
    END IF;

    IF NOT pg_catalog.has_function_privilege('sireader_workforce_maint','sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_workforce_maint','sireader_workforce_private.command_cancel_task_v1(text,text)','EXECUTE')
       OR pg_catalog.has_function_privilege('sireader_workforce_maint','sireader_workforce_private.review_get_pending_v1(text)','EXECUTE') THEN
        RAISE EXCEPTION 'Maintenance function grants are not the exact allowlist';
    END IF;
END;
$function_acl_audit$;

REVOKE CREATE ON SCHEMA sireader_workforce_private FROM sireader_workforce_fn_owner;
REVOKE sireader_workforce_fn_owner FROM postgres;

DO $final_owner_audit$
BEGIN
    -- A CREATEROLE creator retains its implicit ADMIN membership on roles it creates.
    -- This is safe only when that membership cannot SET or INHERIT after the migration.
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members m
        WHERE m.member='sireader_migration_operator'::regrole
          AND m.roleid='sireader_workforce_fn_owner'::regrole
          AND (m.set_option OR m.inherit_option)
    )
       OR pg_catalog.has_schema_privilege('sireader_workforce_fn_owner',
           'sireader_workforce_private','CREATE')
       OR NOT pg_catalog.has_schema_privilege('sireader_workforce_fn_owner',
           'sireader_workforce_private','USAGE') THEN
        RAISE EXCEPTION 'Function owner retained temporary SET/INHERIT/schema CREATE privileges';
    END IF;
END;
$final_owner_audit$;

COMMIT;
