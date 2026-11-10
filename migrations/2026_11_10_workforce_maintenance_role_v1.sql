-- SIREADER Phase 5.2.5 — least-privilege maintenance identity and RLS boundary.
-- Apply only to the local disposable database. Never apply to Supabase LIVE.
BEGIN;

DO $guard$
DECLARE
    role_row record;
    relation_name text;
BEGIN
    IF current_database() <> 'sireader_disposable'
       OR inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN
        RAISE EXCEPTION 'Maintenance role migration is restricted to loopback sireader_disposable';
    END IF;
    IF current_user NOT IN ('postgres', 'sireader_dev')
       OR NOT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'Run this migration only as the local disposable database superuser';
    END IF;

    FOREACH relation_name IN ARRAY ARRAY[
        'workforce_tasks',
        'workforce_task_attempts',
        'workforce_activity_events',
        'workforce_task_reviews',
        'workforce_task_review_payloads'
    ] LOOP
        IF to_regclass('public.' || relation_name) IS NULL THEN
            RAISE EXCEPTION 'Required workforce relation public.% is missing', relation_name;
        END IF;
        IF NOT (SELECT relrowsecurity FROM pg_catalog.pg_class WHERE oid = to_regclass('public.' || relation_name)) THEN
            RAISE EXCEPTION 'RLS must already be enabled on public.%', relation_name;
        END IF;
    END LOOP;

    SELECT * INTO role_row FROM pg_catalog.pg_roles
    WHERE rolname = 'sireader_workforce_maint';

    IF NOT FOUND THEN
        CREATE ROLE sireader_workforce_maint
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS NOINHERIT;
    ELSE
        IF NOT role_row.rolcanlogin OR role_row.rolsuper OR role_row.rolcreatedb
           OR role_row.rolcreaterole OR role_row.rolreplication
           OR role_row.rolbypassrls OR role_row.rolinherit THEN
            RAISE EXCEPTION 'Existing sireader_workforce_maint role violates least privilege';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member = 'sireader_workforce_maint'::regrole
    ) THEN
        RAISE EXCEPTION 'sireader_workforce_maint must not be a member of any other role';
    END IF;
END;
$guard$;

GRANT CONNECT ON DATABASE sireader_disposable TO sireader_workforce_maint;
GRANT USAGE ON SCHEMA public TO sireader_workforce_maint;

-- No blanket grants: only the five maintenance tables are reachable.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sireader_workforce_maint;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM sireader_workforce_maint;

-- A task is in-scope only when both its capability/type and worker assignment
-- belong to the explicit non-trading workforce allowlist used by Phase 5.1.
DROP POLICY IF EXISTS workforce_maint_tasks_select ON public.workforce_tasks;
CREATE POLICY workforce_maint_tasks_select
ON public.workforce_tasks FOR SELECT TO sireader_workforce_maint
USING (
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
        'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
        'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
        'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
        'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
        'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
        'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
    )
    AND (requested_worker_id IS NULL OR requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND (assigned_worker_id IS NULL OR assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
);

DROP POLICY IF EXISTS workforce_maint_tasks_update ON public.workforce_tasks;
CREATE POLICY workforce_maint_tasks_update
ON public.workforce_tasks FOR UPDATE TO sireader_workforce_maint
USING (
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
        'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
        'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
        'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
        'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
        'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
        'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
    )
    AND (requested_worker_id IS NULL OR requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND (assigned_worker_id IS NULL OR assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND (
        (status IN ('CLAIMED','RUNNING') AND lease_expires_at IS NOT NULL AND lease_expires_at <= now())
        OR (status IN ('QUEUED','RETRY_WAIT') AND deadline_at IS NOT NULL AND deadline_at <= now())
        OR (status IN ('WAITING_REVIEW','COMPLETED','FAILED','CANCELLED','DEAD_LETTERED')
            AND payload_expires_at IS NOT NULL AND payload_expires_at <= now()
            AND payload_purged_at IS NULL)
    )
)
WITH CHECK (
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
        'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
        'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
        'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
        'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
        'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
        'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
    )
    AND (requested_worker_id IS NULL OR requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND (assigned_worker_id IS NULL OR assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND (
        (status IN ('RETRY_WAIT','WAITING_REVIEW','DEAD_LETTERED')
            AND lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (status IN ('WAITING_REVIEW','COMPLETED','FAILED','CANCELLED','DEAD_LETTERED')
            AND payload_purged_at IS NOT NULL AND input_payload = '{}'::jsonb AND result_payload IS NULL)
    )
);

DROP POLICY IF EXISTS workforce_maint_attempts_select ON public.workforce_task_attempts;
CREATE POLICY workforce_maint_attempts_select
ON public.workforce_task_attempts FOR SELECT TO sireader_workforce_maint
USING (
    worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist')
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_attempts.task_id
          AND t.required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
          )
    )
);

DROP POLICY IF EXISTS workforce_maint_attempts_update ON public.workforce_task_attempts;
CREATE POLICY workforce_maint_attempts_update
ON public.workforce_task_attempts FOR UPDATE TO sireader_workforce_maint
USING (
    worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist')
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_attempts.task_id
          AND t.status IN ('CLAIMED','RUNNING')
          AND t.lease_token = workforce_task_attempts.lease_token
          AND t.lease_expires_at IS NOT NULL AND t.lease_expires_at <= now()
    )
)
WITH CHECK (
    worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist')
    AND status IN ('TIMED_OUT','UNKNOWN')
    AND error_code IN (
        'LEASE_EXPIRED_BEFORE_PROVIDER','LEASE_EXPIRED_AFTER_PROVIDER_REQUEST',
        'LEASE_EXPIRED_MAX_ATTEMPTS'
    )
);

DROP POLICY IF EXISTS workforce_maint_reviews_select ON public.workforce_task_reviews;
CREATE POLICY workforce_maint_reviews_select
ON public.workforce_task_reviews FOR SELECT TO sireader_workforce_maint
USING (
    EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_reviews.task_id
          AND t.required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
          )
          AND t.task_type IN (
            'INSTRUCTION_INTAKE','ROUTING','TASK_DECOMPOSITION','WORKFLOW_SUPERVISION',
            'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
            'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
            'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
            'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
            'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
            'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
          )
          AND (t.requested_worker_id IS NULL OR t.requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
          AND (t.assigned_worker_id IS NULL OR t.assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    )
);

DROP POLICY IF EXISTS workforce_maint_reviews_update ON public.workforce_task_reviews;
CREATE POLICY workforce_maint_reviews_update
ON public.workforce_task_reviews FOR UPDATE TO sireader_workforce_maint
USING (
    status = 'PENDING' AND review_expires_at <= now()
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_task_reviews.task_id
          AND t.required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
          )
          AND t.task_type IN (
            'INSTRUCTION_INTAKE','ROUTING','TASK_DECOMPOSITION','WORKFLOW_SUPERVISION',
            'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
            'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
            'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
            'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
            'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
            'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
          )
          AND (t.requested_worker_id IS NULL OR t.requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
          AND (t.assigned_worker_id IS NULL OR t.assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    )
)
WITH CHECK (
    status = 'EXPIRED' AND adjudicated_by = 'system'
    AND reason_code = 'REVIEW_TIMED_OUT' AND adjudicated_at IS NOT NULL
);

DROP POLICY IF EXISTS workforce_maint_review_payloads_select ON public.workforce_task_review_payloads;
CREATE POLICY workforce_maint_review_payloads_select
ON public.workforce_task_review_payloads FOR SELECT TO sireader_workforce_maint
USING (
    EXISTS (
        SELECT 1
        FROM public.workforce_task_reviews r
        JOIN public.workforce_tasks t ON t.task_id = r.task_id
        WHERE r.review_id = workforce_task_review_payloads.review_id
          AND r.status IN ('APPROVED','EDITED','REJECTED','EXPIRED')
          AND t.payload_expires_at IS NOT NULL AND t.payload_expires_at <= now()
          AND t.required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
          )
          AND t.task_type IN (
            'INSTRUCTION_INTAKE','ROUTING','TASK_DECOMPOSITION','WORKFLOW_SUPERVISION',
            'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
            'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
            'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
            'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
            'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
            'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
          )
          AND (t.requested_worker_id IS NULL OR t.requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
          AND (t.assigned_worker_id IS NULL OR t.assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    )
);

DROP POLICY IF EXISTS workforce_maint_review_payloads_update ON public.workforce_task_review_payloads;
CREATE POLICY workforce_maint_review_payloads_update
ON public.workforce_task_review_payloads FOR UPDATE TO sireader_workforce_maint
USING (
    payload_purged_at IS NULL
    AND EXISTS (
        SELECT 1
        FROM public.workforce_task_reviews r
        JOIN public.workforce_tasks t ON t.task_id = r.task_id
        WHERE r.review_id = workforce_task_review_payloads.review_id
          AND r.status IN ('APPROVED','EDITED','REJECTED','EXPIRED')
          AND t.payload_expires_at IS NOT NULL AND t.payload_expires_at <= now()
          AND t.required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
          )
          AND t.task_type IN (
            'INSTRUCTION_INTAKE','ROUTING','TASK_DECOMPOSITION','WORKFLOW_SUPERVISION',
            'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
            'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
            'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
            'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
            'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
            'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
          )
          AND (t.requested_worker_id IS NULL OR t.requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
          AND (t.assigned_worker_id IS NULL OR t.assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    )
)
WITH CHECK (
    payload_purged_at IS NOT NULL
    AND original_output IS NULL AND adjusted_output IS NULL
);

DROP POLICY IF EXISTS workforce_maint_activity_insert ON public.workforce_activity_events;
CREATE POLICY workforce_maint_activity_insert
ON public.workforce_activity_events FOR INSERT TO sireader_workforce_maint
WITH CHECK (
    event_type IN ('TASK_WAITING_REVIEW','TASK_RETRIED','TASK_DEAD_LETTERED','HUMAN_REVIEW_EXPIRED')
    AND actor_type = 'SYSTEM'
    AND actor_id IN ('system_reaper','deadline_reaper','system')
    AND task_id IS NOT NULL
    AND (worker_id IS NULL OR worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    AND EXISTS (
        SELECT 1 FROM public.workforce_tasks t
        WHERE t.task_id = workforce_activity_events.task_id
          AND t.required_capability IN (
            'instruction_intake','task_decomposition','workflow_supervision',
            'task_assignment_oversight','result_review','quick_research',
            'information_extraction','summarization','lightweight_classification',
            'data_analysis','comparison','evaluation','structured_reasoning',
            'complex_reasoning','strategic_analysis','deep_review','data_archiving',
            'artifact_indexing','historical_retrieval_simulation','data_organization'
          )
          AND t.task_type IN (
            'INSTRUCTION_INTAKE','ROUTING','TASK_DECOMPOSITION','WORKFLOW_SUPERVISION',
            'TASK_ASSIGNMENT_OVERSIGHT','RESULT_REVIEW','REVIEW','QUICK_RESEARCH',
            'RESEARCH','INFORMATION_EXTRACTION','CLASSIFICATION','SUMMARIZATION',
            'LIGHTWEIGHT_CLASSIFICATION','DATA_ANALYSIS','REASONING','COMPARISON',
            'EVALUATION','STRUCTURED_REASONING','COMPLEX_REASONING','ESCALATION',
            'STRATEGIC_ANALYSIS','DEEP_REVIEW','DATA_ARCHIVING','ARCHIVAL',
            'ARTIFACT_INDEXING','HISTORICAL_RETRIEVAL_SIMULATION','DATA_ORGANIZATION'
          )
          AND (t.requested_worker_id IS NULL OR t.requested_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
          AND (t.assigned_worker_id IS NULL OR t.assigned_worker_id IN ('supervisor','haiku','sonnet','opus','data_archivist'))
    )
);

GRANT SELECT ON public.workforce_tasks, public.workforce_task_attempts,
    public.workforce_task_reviews, public.workforce_task_review_payloads
    TO sireader_workforce_maint;
GRANT UPDATE (status, not_before, failure_code, payload_expires_at, payload_purged_at,
    input_payload, result_payload, lease_token, lease_owner, lease_expires_at, updated_at)
    ON public.workforce_tasks TO sireader_workforce_maint;
GRANT UPDATE (status, error_code)
    ON public.workforce_task_attempts TO sireader_workforce_maint;
GRANT UPDATE (status, adjudicated_by, reason_code, adjudicated_at, updated_at)
    ON public.workforce_task_reviews TO sireader_workforce_maint;
GRANT UPDATE (original_output, adjusted_output, payload_purged_at, updated_at)
    ON public.workforce_task_review_payloads TO sireader_workforce_maint;
GRANT INSERT ON public.workforce_activity_events TO sireader_workforce_maint;
GRANT USAGE ON SEQUENCE public.workforce_activity_events_event_seq_seq TO sireader_workforce_maint;

COMMENT ON ROLE sireader_workforce_maint IS
    'Local SIREADER maintenance-only login; non-trading workforce retention/expiry RLS scope.';

COMMIT;
