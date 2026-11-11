-- SIREADER production migration: generic workforce runtime schema.
-- Production-safe read-model baseline. Deliberately excludes command audit,
-- command login roles, worker login roles, and maintenance privileges.
-- Requires the already-reviewed Phase 3.1 roles: sireader_view_owner and sireader_api_ro.

BEGIN;

DO $guard$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro'
    ) THEN
        RAISE EXCEPTION 'Required Phase 3.1 SIREADER roles are missing';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'sireader_view_owner'
          AND (rolcanlogin OR rolsuper OR rolinherit OR rolcreatedb
               OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'sireader_view_owner role attributes violate least privilege';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'sireader_api_ro'
          AND (NOT rolcanlogin OR rolsuper OR rolinherit OR rolcreatedb
               OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'sireader_api_ro role attributes violate least privilege';
    END IF;

    IF to_regclass('public.workforce_workers') IS NOT NULL
       OR to_regclass('public.workforce_tasks') IS NOT NULL
       OR to_regclass('public.workforce_task_reviews') IS NOT NULL THEN
        RAISE EXCEPTION 'Workforce objects already exist; reconcile production state before applying this baseline';
    END IF;
END;
$guard$;

CREATE TABLE public.workforce_workers (
    worker_id varchar(64) PRIMARY KEY,
    display_name varchar(100) NOT NULL,
    role varchar(160) NOT NULL,
    department varchar(80) NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    accepting_tasks boolean NOT NULL DEFAULT true,
    max_concurrency integer NOT NULL CHECK (max_concurrency BETWEEN 1 AND 32),
    model_tier varchar(20) NOT NULL DEFAULT 'NONE'
        CHECK (model_tier IN ('NONE','FAST','REASONING','ESCALATION')),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.workforce_capabilities (
    capability_id varchar(64) PRIMARY KEY,
    description varchar(300) NOT NULL,
    task_types jsonb NOT NULL CHECK (jsonb_typeof(task_types) = 'array'),
    enabled boolean NOT NULL DEFAULT true,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1)
);

CREATE TABLE public.workforce_worker_capabilities (
    worker_id varchar(64) NOT NULL
        REFERENCES public.workforce_workers(worker_id) ON DELETE CASCADE,
    capability_id varchar(64) NOT NULL
        REFERENCES public.workforce_capabilities(capability_id) ON DELETE RESTRICT,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (worker_id, capability_id)
);

CREATE TABLE public.workforce_heartbeats (
    heartbeat_id varchar(64) PRIMARY KEY,
    worker_id varchar(64) NOT NULL
        REFERENCES public.workforce_workers(worker_id) ON DELETE CASCADE,
    instance_id varchar(100) NOT NULL,
    process_state varchar(20) NOT NULL
        CHECK (process_state IN ('STARTING','READY','DRAINING','STOPPED','FAILED')),
    process_started_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    active_task_count integer NOT NULL DEFAULT 0 CHECK (active_task_count >= 0),
    in_flight_provider_requests integer NOT NULL DEFAULT 0
        CHECK (in_flight_provider_requests >= 0),
    last_error_code varchar(80),
    UNIQUE(worker_id, instance_id)
);
CREATE INDEX ix_workforce_heartbeats_worker_time
    ON public.workforce_heartbeats(worker_id, heartbeat_at DESC);

CREATE TABLE public.workforce_tasks (
    task_id varchar(64) PRIMARY KEY,
    idempotency_key varchar(128) NOT NULL UNIQUE,
    correlation_id varchar(64) NOT NULL,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    task_type varchar(64) NOT NULL,
    title varchar(180) NOT NULL,
    required_capability varchar(64) NOT NULL
        REFERENCES public.workforce_capabilities(capability_id) ON DELETE RESTRICT,
    requested_worker_id varchar(64)
        REFERENCES public.workforce_workers(worker_id) ON DELETE SET NULL,
    assigned_worker_id varchar(64)
        REFERENCES public.workforce_workers(worker_id) ON DELETE SET NULL,
    related_task_id varchar(64)
        REFERENCES public.workforce_tasks(task_id) ON DELETE SET NULL,
    priority integer NOT NULL DEFAULT 50 CHECK (priority BETWEEN 1 AND 100),
    status varchar(24) NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN (
            'QUEUED','CLAIMED','RUNNING','RETRY_WAIT','WAITING_REVIEW',
            'COMPLETED','FAILED','CANCELLED','DEAD_LETTERED'
        )),
    input_payload jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(input_payload) = 'object'),
    result_payload jsonb
        CHECK (result_payload IS NULL OR jsonb_typeof(result_payload) = 'object'),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    not_before timestamptz NOT NULL DEFAULT now(),
    deadline_at timestamptz,
    lease_token varchar(64) UNIQUE,
    lease_owner varchar(100),
    lease_expires_at timestamptz,
    failure_code varchar(80),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    payload_expires_at timestamptz,
    payload_purged_at timestamptz,
    CONSTRAINT ck_workforce_task_deadline
        CHECK (deadline_at IS NULL OR deadline_at > created_at),
    CONSTRAINT ck_workforce_task_payload_purged_empty
        CHECK (
            payload_purged_at IS NULL OR (
                input_payload = '{}'::jsonb
                AND result_payload IS NULL
                AND status IN ('WAITING_REVIEW','COMPLETED','FAILED','CANCELLED','DEAD_LETTERED')
            )
        )
);

CREATE INDEX ix_workforce_task_payload_expiry
    ON public.workforce_tasks(payload_expires_at)
    WHERE payload_expires_at IS NOT NULL AND payload_purged_at IS NULL;
CREATE INDEX ix_workforce_task_queue
    ON public.workforce_tasks(status, not_before, priority DESC, created_at);
CREATE INDEX ix_workforce_task_worker_status
    ON public.workforce_tasks(assigned_worker_id, status);
CREATE INDEX ix_workforce_task_lease_expiry
    ON public.workforce_tasks(lease_expires_at)
    WHERE status IN ('CLAIMED','RUNNING');
CREATE INDEX ix_workforce_task_correlation
    ON public.workforce_tasks(correlation_id);
CREATE UNIQUE INDEX uq_workforce_tasks_related_task
    ON public.workforce_tasks(related_task_id)
    WHERE related_task_id IS NOT NULL;

CREATE TABLE public.workforce_task_attempts (
    attempt_id varchar(64) PRIMARY KEY,
    task_id varchar(64) NOT NULL
        REFERENCES public.workforce_tasks(task_id) ON DELETE CASCADE,
    attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 10),
    worker_id varchar(64) NOT NULL
        REFERENCES public.workforce_workers(worker_id) ON DELETE RESTRICT,
    lease_token varchar(64) NOT NULL UNIQUE,
    status varchar(24) NOT NULL
        CHECK (status IN (
            'CLAIMED','STARTED','PROVIDER_REQUESTED','COMPLETED',
            'FAILED','TIMED_OUT','UNKNOWN','CANCELLED'
        )),
    provider_request_id varchar(64) UNIQUE,
    provider_started_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    billing_currency varchar(3)
        CHECK (billing_currency IS NULL OR billing_currency IN ('IDR','USD')),
    cost_amount numeric(18,6) CHECK (cost_amount IS NULL OR cost_amount >= 0),
    error_code varchar(80),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(task_id, attempt_number)
);
CREATE INDEX ix_workforce_attempt_task_created
    ON public.workforce_task_attempts(task_id, created_at DESC);

CREATE TABLE public.workforce_activity_events (
    event_seq bigint GENERATED ALWAYS AS IDENTITY,
    event_id varchar(64) PRIMARY KEY,
    event_type varchar(40) NOT NULL
        CHECK (event_type IN (
            'TASK_ENQUEUED','TASK_CLAIMED','TASK_STARTED','PROVIDER_REQUESTED',
            'TASK_RETRIED','TASK_COMPLETED','TASK_FAILED','TASK_CANCELLED',
            'TASK_WAITING_REVIEW','TASK_DEAD_LETTERED','WORKER_HEARTBEAT',
            'WORKER_STATE_CHANGED','HUMAN_REVIEW_QUEUED',
            'HUMAN_REVIEW_APPROVED','HUMAN_REVIEW_EDITED',
            'HUMAN_REVIEW_REJECTED','HUMAN_REVIEW_EXPIRED'
        )),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_type varchar(32) NOT NULL DEFAULT 'LEGACY_UNKNOWN'
        CHECK (actor_type IN ('SYSTEM','WORKER','OPERATOR','LEGACY_UNKNOWN')),
    actor_id varchar(128),
    worker_id varchar(64),
    task_id varchar(64)
        REFERENCES public.workforce_tasks(task_id) ON DELETE SET NULL,
    attempt_id varchar(64)
        REFERENCES public.workforce_task_attempts(attempt_id) ON DELETE SET NULL,
    related_task_id varchar(64)
        REFERENCES public.workforce_tasks(task_id) ON DELETE SET NULL,
    correlation_id varchar(64) NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT uq_workforce_activity_event_seq UNIQUE(event_seq)
);
CREATE INDEX ix_workforce_activity_created
    ON public.workforce_activity_events(occurred_at DESC);
CREATE INDEX ix_workforce_activity_task_seq
    ON public.workforce_activity_events(task_id, event_seq ASC);
CREATE INDEX ix_workforce_activity_related_task
    ON public.workforce_activity_events(related_task_id);
CREATE INDEX ix_workforce_activity_actor
    ON public.workforce_activity_events(actor_type, actor_id);
CREATE INDEX ix_workforce_activity_correlation
    ON public.workforce_activity_events(correlation_id);

-- Canonical roster only. No task history or local QA fixture rows are imported.
INSERT INTO public.workforce_workers
    (worker_id, display_name, role, department, max_concurrency, model_tier)
VALUES
    ('supervisor','Supervisor AI','Central Router & Task Orchestrator','Command',2,'NONE'),
    ('haiku','Haiku Worker','Fast Research & Intake Specialist','Research',2,'FAST'),
    ('sonnet','Sonnet Worker','Deep Quantitative & Structural Analyst','Analysis',1,'REASONING'),
    ('opus','Opus Worker','High-Complexity Escalation Architect','Strategy',1,'ESCALATION'),
    ('decision_engineer','Decision Engineer','Risk Guardrail & Rule Validator','Trading Operations',1,'NONE'),
    ('market_monitor','Market Monitor','Market Feed & Volatility Observer','Market Monitoring',1,'NONE'),
    ('data_archivist','Data Archivist','Audit Ledger & Historical State Curator','Archive',1,'NONE');

INSERT INTO public.workforce_capabilities
    (capability_id, description, task_types)
VALUES
    ('instruction_intake','Normalize operator instructions into bounded workforce requests.','["INSTRUCTION_INTAKE","ROUTING"]'),
    ('task_decomposition','Decompose a non-trading work request into ordered subtasks.','["TASK_DECOMPOSITION","ROUTING"]'),
    ('workflow_supervision','Observe progress and checkpoint multi-stage work.','["WORKFLOW_SUPERVISION","ROUTING"]'),
    ('task_assignment_oversight','Check task-to-worker assignment suitability.','["TASK_ASSIGNMENT_OVERSIGHT","ROUTING"]'),
    ('result_review','Review completeness and quality of a non-trading work result.','["RESULT_REVIEW","REVIEW"]'),
    ('quick_research','Perform bounded fast research over supplied content.','["QUICK_RESEARCH","RESEARCH"]'),
    ('information_extraction','Extract structured facts from supplied content.','["INFORMATION_EXTRACTION","CLASSIFICATION"]'),
    ('summarization','Summarize supplied non-sensitive work content.','["SUMMARIZATION","CLASSIFICATION"]'),
    ('lightweight_classification','Classify work items with a fixed schema.','["LIGHTWEIGHT_CLASSIFICATION","CLASSIFICATION"]'),
    ('data_analysis','Analyze bounded structured datasets.','["DATA_ANALYSIS","REASONING"]'),
    ('comparison','Compare supplied alternatives against explicit criteria.','["COMPARISON","REASONING"]'),
    ('evaluation','Evaluate supplied artifacts against a checklist.','["EVALUATION","REASONING"]'),
    ('structured_reasoning','Perform multi-step reasoning with cited input evidence.','["STRUCTURED_REASONING","REASONING"]'),
    ('complex_reasoning','Handle explicitly escalated complex non-trading analysis.','["COMPLEX_REASONING","ESCALATION"]'),
    ('strategic_analysis','Produce strategy alternatives for an approved work objective.','["STRATEGIC_ANALYSIS","ESCALATION"]'),
    ('deep_review','Perform independent deep review of a supplied artifact.','["DEEP_REVIEW","ESCALATION"]'),
    ('decision_evaluation','Evaluate non-execution decisions against supplied policy.','["DECISION_EVALUATION","REVIEW"]'),
    ('rule_validation','Validate an artifact against explicit deterministic rules.','["RULE_VALIDATION","DECISION_CHECK"]'),
    ('risk_assessment_simulation','Evaluate hypothetical risk scenarios only; never place orders.','["RISK_ASSESSMENT_SIMULATION","DECISION_CHECK"]'),
    ('decision_consistency_checks','Check consistency of documented decisions without executing them.','["DECISION_CONSISTENCY_CHECKS","DECISION_CHECK"]'),
    ('market_monitoring_simulation','Inspect simulated or supplied market observations only.','["MARKET_MONITORING_SIMULATION","MARKET_WATCH"]'),
    ('market_condition_detection','Detect patterns in supplied market observations without trading.','["MARKET_CONDITION_DETECTION","MARKET_WATCH"]'),
    ('anomaly_detection_simulation','Identify anomalies in simulated/supplied market data.','["ANOMALY_DETECTION_SIMULATION","MARKET_WATCH"]'),
    ('market_event_reporting','Report observed market events without execution authority.','["MARKET_EVENT_REPORTING","MARKET_WATCH"]'),
    ('data_archiving','Archive authorized work artifacts with provenance.','["DATA_ARCHIVING","ARCHIVAL"]'),
    ('artifact_indexing','Index authorized artifact metadata.','["ARTIFACT_INDEXING","ARCHIVAL"]'),
    ('historical_retrieval_simulation','Retrieve historical test/demo artifacts only.','["HISTORICAL_RETRIEVAL_SIMULATION","ARCHIVAL"]'),
    ('data_organization','Organize approved data artifacts and metadata.','["DATA_ORGANIZATION","ARCHIVAL"]');

INSERT INTO public.workforce_worker_capabilities (worker_id, capability_id)
VALUES
    ('supervisor','instruction_intake'),('supervisor','task_decomposition'),
    ('supervisor','workflow_supervision'),('supervisor','task_assignment_oversight'),
    ('supervisor','result_review'),
    ('haiku','quick_research'),('haiku','information_extraction'),
    ('haiku','summarization'),('haiku','lightweight_classification'),
    ('sonnet','data_analysis'),('sonnet','comparison'),('sonnet','evaluation'),
    ('sonnet','structured_reasoning'),
    ('opus','complex_reasoning'),('opus','strategic_analysis'),
    ('opus','deep_review'),('opus','decision_evaluation'),
    ('decision_engineer','rule_validation'),('decision_engineer','decision_evaluation'),
    ('decision_engineer','risk_assessment_simulation'),
    ('decision_engineer','decision_consistency_checks'),
    ('market_monitor','market_monitoring_simulation'),
    ('market_monitor','market_condition_detection'),
    ('market_monitor','anomaly_detection_simulation'),
    ('market_monitor','market_event_reporting'),
    ('data_archivist','data_archiving'),('data_archivist','artifact_indexing'),
    ('data_archivist','historical_retrieval_simulation'),
    ('data_archivist','data_organization');

-- Notify listeners with a fixed resource key only; no row data is sent.
CREATE FUNCTION public.sireader_notify_workforce_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        PERFORM pg_catalog.pg_notify('sireader_observability_v1', 'workforce');
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' AND pg_catalog.to_jsonb(OLD) = pg_catalog.to_jsonb(NEW) THEN
        RETURN NEW;
    END IF;
    PERFORM pg_catalog.pg_notify('sireader_observability_v1', 'workforce');
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$function$;

DO $triggers$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'workforce_workers','workforce_capabilities','workforce_worker_capabilities',
        'workforce_heartbeats','workforce_tasks','workforce_task_attempts',
        'workforce_activity_events'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER sireader_workforce_notify AFTER INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.sireader_notify_workforce_v1()',
            table_name
        );
        EXECUTE format(
            'CREATE TRIGGER sireader_workforce_truncate AFTER TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_notify_workforce_v1()',
            table_name
        );
    END LOOP;
END;
$triggers$;

-- Activity is an append-only audit record.
CREATE FUNCTION public.sireader_reject_activity_event_mutation_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'workforce activity events are append-only';
END;
$function$;

CREATE TRIGGER sireader_activity_event_immutable
BEFORE UPDATE OR DELETE ON public.workforce_activity_events
FOR EACH ROW EXECUTE FUNCTION public.sireader_reject_activity_event_mutation_v1();

CREATE TRIGGER sireader_activity_event_truncate_immutable
BEFORE TRUNCATE ON public.workforce_activity_events
FOR EACH STATEMENT EXECUTE FUNCTION public.sireader_reject_activity_event_mutation_v1();

-- Trigger functions are not callable directly by client or API roles.
REVOKE ALL ON FUNCTION public.sireader_notify_workforce_v1()
    FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;
REVOKE ALL ON FUNCTION public.sireader_reject_activity_event_mutation_v1()
    FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;
REVOKE ALL ON SEQUENCE public.workforce_activity_events_event_seq_seq
    FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro, sireader_view_owner;

-- RLS deny-by-default for every caller except the non-login view owner.
ALTER TABLE public.workforce_workers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_worker_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_task_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_activity_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY workforce_view_owner_select
    ON public.workforce_workers FOR SELECT TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_capabilities FOR SELECT TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_worker_capabilities FOR SELECT TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_heartbeats FOR SELECT TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_tasks FOR SELECT TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_task_attempts FOR SELECT TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_activity_events FOR SELECT TO sireader_view_owner USING (true);

-- Remove Supabase client roles and API login from the new base tables.
REVOKE ALL ON TABLE
    public.workforce_workers, public.workforce_capabilities,
    public.workforce_worker_capabilities, public.workforce_heartbeats,
    public.workforce_tasks, public.workforce_task_attempts,
    public.workforce_activity_events
FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;

GRANT USAGE ON SCHEMA public TO sireader_view_owner, sireader_api_ro;

-- Exact column-level SELECT allowlist used by the security-barrier API views.
GRANT SELECT (
    worker_id, display_name, role, department, enabled, accepting_tasks,
    max_concurrency, model_tier, schema_version, created_at, updated_at
) ON public.workforce_workers TO sireader_view_owner;
GRANT SELECT (
    capability_id, description, task_types, enabled, schema_version
) ON public.workforce_capabilities TO sireader_view_owner;
GRANT SELECT (worker_id, capability_id, enabled)
    ON public.workforce_worker_capabilities TO sireader_view_owner;
GRANT SELECT (
    worker_id, process_state, heartbeat_at,
    active_task_count, in_flight_provider_requests
) ON public.workforce_heartbeats TO sireader_view_owner;
GRANT SELECT (
    task_id, correlation_id, schema_version, task_type, title,
    required_capability, requested_worker_id, assigned_worker_id,
    related_task_id, priority, status, attempts, max_attempts, not_before,
    deadline_at, lease_expires_at, failure_code, created_at, updated_at,
    completed_at
) ON public.workforce_tasks TO sireader_view_owner;
GRANT SELECT (
    attempt_id, task_id, attempt_number, worker_id, status,
    provider_request_id, billing_currency, cost_amount, error_code,
    started_at, finished_at, created_at
) ON public.workforce_task_attempts TO sireader_view_owner;
GRANT SELECT (
    event_seq, event_id, event_type, occurred_at, actor_type, actor_id,
    worker_id, task_id, attempt_id, related_task_id, correlation_id, payload
) ON public.workforce_activity_events TO sireader_view_owner;

COMMIT;
