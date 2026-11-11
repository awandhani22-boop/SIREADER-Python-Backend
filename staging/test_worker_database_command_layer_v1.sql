-- Transactional acceptance tests for 2026_11_11_worker_database_command_layer_v1.sql.
-- All synthetic task writes are rolled back at the end.
\set ON_ERROR_STOP on
BEGIN;

DO $acl$
DECLARE
    v_worker record;
BEGIN
    FOR v_worker IN
        SELECT rolname FROM pg_catalog.pg_roles WHERE rolname LIKE 'sireader_worker_%'
    LOOP
        IF pg_catalog.has_table_privilege(v_worker.rolname,'public.workforce_tasks','SELECT')
           OR pg_catalog.has_table_privilege(v_worker.rolname,'public.workforce_tasks','INSERT')
           OR pg_catalog.has_table_privilege(v_worker.rolname,'public.workforce_tasks','UPDATE')
           OR pg_catalog.has_table_privilege(v_worker.rolname,'public.workforce_task_attempts','SELECT')
           OR pg_catalog.has_table_privilege(v_worker.rolname,'public.workforce_activity_events','INSERT')
           OR pg_catalog.has_schema_privilege(v_worker.rolname,'public','CREATE') THEN
            RAISE EXCEPTION 'Worker role % has an unexpected base table/schema grant', v_worker.rolname;
        END IF;
    END LOOP;

    IF pg_catalog.has_function_privilege(
        'sireader_worker_decision_engineer',
        'sireader_workforce_private.worker_reserve_provider_slot_v1(text,text,text,text,text)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Decision Engineer must not reserve model provider slots';
    END IF;

    IF pg_catalog.has_function_privilege(
        'sireader_api_ro',
        'sireader_workforce_private.worker_claim_next_v1(text,text,integer,integer)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Read-only API must not execute worker commands';
    END IF;
END;
$acl$;

CREATE ROLE "sireader_worker_haiku.pooler" NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT sireader_worker_haiku TO "sireader_worker_haiku.pooler";
SET SESSION AUTHORIZATION "sireader_worker_haiku.pooler";
DO $pooler_identity$
DECLARE
    v_contract jsonb;
BEGIN
    v_contract := sireader_workforce_private.worker_get_contract_v1('haiku');
    IF v_contract->>'worker_id' <> 'haiku' THEN
        RAISE EXCEPTION 'Supabase-style pooler identity suffix was not normalized';
    END IF;
END;
$pooler_identity$;
RESET SESSION AUTHORIZATION;
DROP ROLE "sireader_worker_haiku.pooler";

INSERT INTO public.workforce_tasks(
    task_id,idempotency_key,correlation_id,task_type,title,required_capability,
    requested_worker_id,priority,status,input_payload,result_payload,attempts,max_attempts,
    not_before,created_at,updated_at,completed_at,payload_expires_at
)
VALUES
('stage_writer_haiku_01','stage_writer_idem_haiku_01','stage_writer_corr_haiku_01',
 'QUICK_RESEARCH','PG17 writer test — Haiku provider slot','quick_research',
 'haiku',80,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_sonnet_01','stage_writer_idem_sonnet_01','stage_writer_corr_sonnet_01',
 'COMPARISON','PG17 writer test — Sonnet provider contention','comparison',
 'sonnet',80,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_opus_01','stage_writer_idem_opus_01','stage_writer_corr_opus_01',
 'COMPLEX_REASONING','PG17 writer test — Opus mandatory review','complex_reasoning',
 'opus',80,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_archive_01','stage_writer_idem_archive_01','stage_writer_corr_archive_01',
 'DATA_ARCHIVING','PG17 writer test — normal completion','data_archiving',
 'data_archivist',80,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_output_pii_01','stage_writer_idem_output_pii_01','stage_writer_corr_output_pii_01',
 'DATA_ARCHIVING','PG17 writer test — output PII quarantine','data_archiving',
 'data_archivist',70,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_sonnet_truncated_01','stage_writer_idem_sonnet_truncated_01','stage_writer_corr_sonnet_truncated_01',
 'COMPARISON','PG17 writer test — truncated LIVE output','comparison',
 'sonnet',100,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,1,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_opus_trading_01','stage_writer_idem_opus_trading_01','stage_writer_corr_opus_trading_01',
 'DECISION_EVALUATION','PG17 writer test — trading capability provider denial','decision_evaluation',
 'opus',70,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_writer_sonnet_spoof_01','stage_writer_idem_sonnet_spoof_01','stage_writer_corr_sonnet_spoof_01',
 'COMPARISON','PG17 writer test — forged LIVE provenance','comparison',
 'sonnet',70,'QUEUED','{"source":"SIMULATED"}'::jsonb,NULL,0,2,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),NULL,NULL),
('stage_purge_untrusted_01','stage_purge_untrusted_idem_01','stage_purge_untrusted_corr_01',
 'DATA_ARCHIVING','PG17 writer test — unauthorized retention attempt','data_archiving',
 'data_archivist',10,'COMPLETED','{"source":"SIMULATED"}'::jsonb,'{"source":"SIMULATED","summary":"untrusted purge fixture"}'::jsonb,1,1,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),clock_timestamp()-interval '2 days',clock_timestamp()-interval '1 day'),
('stage_purge_maintenance_01','stage_purge_maintenance_idem_01','stage_purge_maintenance_corr_01',
 'DATA_ARCHIVING','PG17 writer test — authorized maintenance retention','data_archiving',
 'data_archivist',10,'COMPLETED','{"source":"SIMULATED"}'::jsonb,'{"source":"SIMULATED","summary":"maintenance purge fixture"}'::jsonb,1,1,
 clock_timestamp(),clock_timestamp(),clock_timestamp(),clock_timestamp()-interval '2 days',clock_timestamp()-interval '1 day');

-- A truncated provider result is quarantined even when the capability policy is sampled.
-- This is a synthetic database fixture only; no HTTP request or model inference occurs.
SET SESSION AUTHORIZATION sireader_worker_sonnet;
DO $sonnet_truncated$
DECLARE
    v_claim jsonb;
    v_result jsonb;
    v_task_id text;
    v_token text;
    v_instance text := 'stage-instance-sonnet-truncated-001';
BEGIN
    PERFORM sireader_workforce_private.worker_heartbeat_v1(
        'sonnet',v_instance,'READY',clock_timestamp()-interval '30 seconds',
        clock_timestamp(),0,0
    );
    v_claim := sireader_workforce_private.worker_claim_next_v1('sonnet',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_sonnet_truncated_01' THEN
        RAISE EXCEPTION 'Sonnet did not claim the synthetic truncation task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('sonnet',v_instance,v_task_id,v_token);
    v_result := sireader_workforce_private.worker_reserve_provider_slot_v1(
        'sonnet',v_instance,v_task_id,v_token,'stage-provider-sonnet-truncated-01'
    );
    IF (v_result->>'acquired')::boolean IS NOT TRUE THEN
        RAISE EXCEPTION 'Synthetic provider slot for truncated-output fixture was not acquired';
    END IF;
    v_result := sireader_workforce_private.worker_complete_task_v1(
        'sonnet',v_instance,v_task_id,v_token,
        '{"source":"LIVE","output_truncated":true,"summary":"synthetic truncated result"}'::jsonb,
        'USD',0.000001,
        '{"policy_version":"2026.11.10-harness.1","sensitive_action":"ALLOW","model_tier":"REASONING","reason_code":"POLICY_ALLOWED","redacted_fields":0}'::jsonb
    );
    IF v_result->>'status' <> 'COMPLETED'
       OR (v_result->>'requires_review')::boolean IS NOT TRUE
       OR v_result->>'review_id' IS NULL THEN
        RAISE EXCEPTION 'Truncated LIVE output must enter human review quarantine';
    END IF;
END;
$sonnet_truncated$;
RESET SESSION AUTHORIZATION;
DO $sonnet_truncated_audit$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.workforce_tasks
        WHERE task_id='stage_writer_sonnet_truncated_01' AND result_payload IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Truncated output escaped the review quarantine';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM public.workforce_task_reviews r
        JOIN public.workforce_activity_events e
          ON e.task_id=r.task_id
         AND e.event_type='HUMAN_REVIEW_QUEUED'
         AND e.payload->>'review_id'=r.review_id
        WHERE r.task_id='stage_writer_sonnet_truncated_01'
          AND r.status='PENDING'
          AND e.payload->>'trigger_reason'='OUTPUT_TRUNCATED_REVIEW'
    ) THEN
        RAISE EXCEPTION 'Truncated output review audit reason is missing';
    END IF;
END;
$sonnet_truncated_audit$;

SET SESSION AUTHORIZATION sireader_worker_haiku;
DO $haiku$
DECLARE
    v_contract jsonb;
    v_claim jsonb;
    v_result jsonb;
    v_token text;
    v_task_id text;
    v_instance text := 'stage-instance-haiku-001';
BEGIN
    v_contract := sireader_workforce_private.worker_get_contract_v1('haiku');
    IF v_contract->>'worker_id' <> 'haiku' THEN
        RAISE EXCEPTION 'Haiku worker contract identity mismatch';
    END IF;

    PERFORM sireader_workforce_private.worker_heartbeat_v1(
        'haiku',v_instance,'READY',clock_timestamp()-interval '30 seconds',
        clock_timestamp(),0,0
    );
    v_claim := sireader_workforce_private.worker_claim_next_v1('haiku',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_haiku_01' THEN
        RAISE EXCEPTION 'Haiku did not claim its requested synthetic task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';

    PERFORM sireader_workforce_private.worker_start_v1('haiku',v_instance,v_task_id,v_token);
    v_result := sireader_workforce_private.worker_reserve_provider_slot_v1(
        'haiku',v_instance,v_task_id,v_token,'stage-provider-haiku-01'
    );
    IF (v_result->>'acquired')::boolean IS NOT TRUE THEN
        RAISE EXCEPTION 'First global provider slot was not acquired';
    END IF;

    v_result := sireader_workforce_private.worker_fail_task_v1(
        'haiku',v_instance,v_task_id,v_token,
        'STAGING_PROVIDER_OUTCOME_UNKNOWN',false,true,30
    );
    IF v_result->>'status' <> 'WAITING_REVIEW' THEN
        RAISE EXCEPTION 'Unknown provider outcome was not quarantined';
    END IF;
END;
$haiku$;
RESET SESSION AUTHORIZATION;

SET SESSION AUTHORIZATION sireader_worker_sonnet;
DO $sonnet$
DECLARE
    v_claim jsonb;
    v_result jsonb;
    v_token text;
    v_task_id text;
    v_instance text := 'stage-instance-sonnet-001';
    v_rejected boolean := false;
BEGIN
    BEGIN
        PERFORM sireader_workforce_private.worker_get_contract_v1('haiku');
    EXCEPTION WHEN OTHERS THEN
        v_rejected := SQLERRM LIKE '%Supplied worker identity does not match%';
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Cross-worker identity spoof was not rejected';
    END IF;

    v_rejected := false;
    BEGIN
        PERFORM 1 FROM public.workforce_tasks;
    EXCEPTION WHEN insufficient_privilege THEN
        v_rejected := true;
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Worker role unexpectedly read the base task table';
    END IF;

    PERFORM sireader_workforce_private.worker_heartbeat_v1(
        'sonnet',v_instance,'READY',clock_timestamp()-interval '30 seconds',
        clock_timestamp(),0,0
    );
    v_claim := sireader_workforce_private.worker_claim_next_v1('sonnet',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_sonnet_01' THEN
        RAISE EXCEPTION 'Sonnet did not claim its requested synthetic task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('sonnet',v_instance,v_task_id,v_token);

    v_result := sireader_workforce_private.worker_reserve_provider_slot_v1(
        'sonnet',v_instance,v_task_id,v_token,'stage-provider-sonnet-01'
    );
    IF (v_result->>'acquired')::boolean IS NOT FALSE THEN
        RAISE EXCEPTION 'Second global provider slot should remain blocked by UNKNOWN';
    END IF;

    v_result := sireader_workforce_private.worker_fail_task_v1(
        'sonnet',v_instance,v_task_id,v_token,'STAGING_PROVIDER_SLOT_BUSY',false,false,30
    );
    IF v_result->>'status' <> 'FAILED' THEN
        RAISE EXCEPTION 'Non-provider failure did not persist as FAILED';
    END IF;

    v_claim := sireader_workforce_private.worker_claim_next_v1('sonnet',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_sonnet_spoof_01' THEN
        RAISE EXCEPTION 'Sonnet did not claim the synthetic provenance test task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('sonnet',v_instance,v_task_id,v_token);

    v_rejected := false;
    BEGIN
        PERFORM sireader_workforce_private.worker_complete_task_v1(
            'sonnet',v_instance,v_task_id,v_token,
            '{"source":"LIVE","summary":"forged unreserved LIVE provenance"}'::jsonb,
            NULL,NULL,
            '{"policy_version":"2026.11.10-harness.1","sensitive_action":"ALLOW","model_tier":"REASONING","reason_code":"POLICY_ALLOWED","redacted_fields":0}'::jsonb
        );
    EXCEPTION WHEN raise_exception THEN
        v_rejected := SQLERRM LIKE '%Unreserved task completion cannot claim LIVE provenance%';
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Unreserved LIVE provenance was not rejected';
    END IF;

    v_rejected := false;
    BEGIN
        PERFORM sireader_workforce_private.worker_complete_task_v1(
            'sonnet',v_instance,v_task_id,v_token,
            '{"source":"SIMULATED","summary":"forged policy tier"}'::jsonb,
            NULL,NULL,
            '{"policy_version":"2026.11.10-harness.1","sensitive_action":"ALLOW","model_tier":"FAST","reason_code":"POLICY_ALLOWED","redacted_fields":0}'::jsonb
        );
    EXCEPTION WHEN raise_exception THEN
        v_rejected := SQLERRM LIKE '%model tier does not match its registered profile%';
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Mismatched worker policy model tier was not rejected';
    END IF;

    v_result := sireader_workforce_private.worker_fail_task_v1(
        'sonnet',v_instance,v_task_id,v_token,'STAGING_PROVENANCE_REJECTED',false,false,30
    );
    IF v_result->>'status' <> 'FAILED' THEN
        RAISE EXCEPTION 'Rejected provenance fixture did not close as FAILED';
    END IF;
END;
$sonnet$;
RESET SESSION AUTHORIZATION;

SET SESSION AUTHORIZATION sireader_worker_opus;
DO $opus$
DECLARE
    v_claim jsonb;
    v_result jsonb;
    v_task_id text;
    v_token text;
    v_instance text := 'stage-instance-opus-001';
    v_review_id text;
    v_rejected boolean := false;
BEGIN
    PERFORM sireader_workforce_private.worker_heartbeat_v1(
        'opus',v_instance,'READY',clock_timestamp()-interval '30 seconds',
        clock_timestamp(),0,0
    );
    v_claim := sireader_workforce_private.worker_claim_next_v1('opus',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_opus_01' THEN
        RAISE EXCEPTION 'Opus did not claim its requested synthetic task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('opus',v_instance,v_task_id,v_token);

    v_result := sireader_workforce_private.worker_complete_task_v1(
        'opus',v_instance,v_task_id,v_token,
        '{"source":"SIMULATED","summary":"review-quarantine test","api_token":"sk-abcdefghijklmnopqrstuvwxyz0123456789"}'::jsonb,
        NULL,NULL,
        '{"policy_version":"2026.11.10-harness.1","sensitive_action":"ALLOW","model_tier":"ESCALATION","reason_code":"POLICY_ALLOWED","redacted_fields":0}'::jsonb
    );
    IF v_result->>'status' <> 'COMPLETED'
       OR (v_result->>'requires_review')::boolean IS NOT TRUE
       OR v_result->>'review_id' IS NULL THEN
        RAISE EXCEPTION 'Opus mandatory review boundary was not created';
    END IF;
    v_review_id := v_result->>'review_id';

    v_claim := sireader_workforce_private.worker_claim_next_v1('opus',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_opus_trading_01' THEN
        RAISE EXCEPTION 'Opus did not claim the synthetic trading-capability guard task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('opus',v_instance,v_task_id,v_token);

    v_rejected := false;
    BEGIN
        PERFORM sireader_workforce_private.worker_reserve_provider_slot_v1(
            'opus',v_instance,v_task_id,v_token,'stage-provider-opus-trading-01'
        );
    EXCEPTION WHEN raise_exception THEN
        v_rejected := SQLERRM LIKE '%Trading-related capabilities are not eligible%';
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Opus reserved an AI provider slot for a trading-related capability';
    END IF;
    v_result := sireader_workforce_private.worker_fail_task_v1(
        'opus',v_instance,v_task_id,v_token,'STAGING_TRADING_PROVIDER_DENIED',false,false,30
    );
    IF v_result->>'status' <> 'FAILED' THEN
        RAISE EXCEPTION 'Trading-capability provider denial fixture did not close as FAILED';
    END IF;
END;
$opus$;
RESET SESSION AUTHORIZATION;
DO $opus_audit$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_task_reviews r
        JOIN public.workforce_task_review_payloads p ON p.review_id=r.review_id
        WHERE r.task_id='stage_writer_opus_01' AND r.status='PENDING'
          AND r.original_output_sha256 ~ '^[0-9a-f]{64}$'
          AND p.original_output->>'api_token'='[REDACTED]'
          AND p.adjusted_output IS NULL AND p.payload_purged_at IS NULL
    ) THEN
        RAISE EXCEPTION 'Opus review metadata/payload quarantine or sanitizer failed';
    END IF;
    IF EXISTS (SELECT 1 FROM public.workforce_tasks WHERE task_id='stage_writer_opus_01' AND result_payload IS NOT NULL) THEN
        RAISE EXCEPTION 'Review-required result escaped quarantine';
    END IF;
END;
$opus_audit$;

SET SESSION AUTHORIZATION sireader_worker_data_archivist;
DO $archivist$
DECLARE
    v_claim jsonb;
    v_result jsonb;
    v_task_id text;
    v_token text;
    v_instance text := 'stage-instance-archivist-001';
BEGIN
    PERFORM sireader_workforce_private.worker_heartbeat_v1(
        'data_archivist',v_instance,'READY',clock_timestamp()-interval '30 seconds',
        clock_timestamp(),0,0
    );
    v_claim := sireader_workforce_private.worker_claim_next_v1('data_archivist',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_archive_01' THEN
        RAISE EXCEPTION 'Data Archivist did not claim its requested synthetic task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('data_archivist',v_instance,v_task_id,v_token);

    v_result := sireader_workforce_private.worker_complete_task_v1(
        'data_archivist',v_instance,v_task_id,v_token,
        '{"source":"SIMULATED","summary":"normal completion release"}'::jsonb,
        NULL,NULL,
        '{"policy_version":"2026.11.10-harness.1","sensitive_action":"ALLOW","model_tier":"NONE","reason_code":"POLICY_ALLOWED","redacted_fields":0}'::jsonb
    );
    IF v_result->>'status' <> 'COMPLETED'
       OR (v_result->>'requires_review')::boolean IS NOT FALSE
       OR v_result->>'review_id' IS NOT NULL THEN
        RAISE EXCEPTION 'Normal completion should release without a review row';
    END IF;
END;
$archivist$;
DO $archivist_pii$
DECLARE
    v_claim jsonb;
    v_result jsonb;
    v_task_id text;
    v_token text;
    v_instance text := 'stage-instance-archivist-001';
BEGIN
    v_claim := sireader_workforce_private.worker_claim_next_v1('data_archivist',v_instance,45,60);
    IF v_claim IS NULL OR v_claim->'task'->>'task_id' <> 'stage_writer_output_pii_01' THEN
        RAISE EXCEPTION 'Data Archivist did not claim the output-PII synthetic task';
    END IF;
    v_task_id := v_claim->'task'->>'task_id';
    v_token := v_claim->'attempt'->>'lease_token';
    PERFORM sireader_workforce_private.worker_start_v1('data_archivist',v_instance,v_task_id,v_token);
    v_result := sireader_workforce_private.worker_complete_task_v1(
        'data_archivist',v_instance,v_task_id,v_token,
        '{"source":"SIMULATED","customer_email":"review@example.test"}'::jsonb,
        NULL,NULL,
        '{"policy_version":"2026.11.10-harness.1","sensitive_action":"ALLOW","model_tier":"NONE","reason_code":"POLICY_ALLOWED","redacted_fields":0}'::jsonb
    );
    IF (v_result->>'requires_review')::boolean IS NOT TRUE OR v_result->>'review_id' IS NULL THEN
        RAISE EXCEPTION 'Output PII did not force database review quarantine';
    END IF;
END;
$archivist_pii$;
RESET SESSION AUTHORIZATION;
DO $archivist_pii_audit$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_task_reviews r
        JOIN public.workforce_task_review_payloads p ON p.review_id=r.review_id
        WHERE r.task_id='stage_writer_output_pii_01' AND r.status='PENDING'
          AND p.original_output->>'customer_email'='review@example.test'
    ) THEN
        RAISE EXCEPTION 'Output PII review quarantine row is missing';
    END IF;
END;
$archivist_pii_audit$;
DO $archivist_audit$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_tasks
        WHERE task_id='stage_writer_archive_01' AND status='COMPLETED'
          AND result_payload->>'summary'='normal completion release'
          AND lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL
    ) THEN
        RAISE EXCEPTION 'Normal completion release or lease clearing failed';
    END IF;
END;
$archivist_audit$;

-- Authenticated worker roles cannot forge activity rows via direct INSERT.
SET SESSION AUTHORIZATION sireader_worker_haiku;
DO $event_acl$
DECLARE
    v_rejected boolean := false;
BEGIN
    BEGIN
        INSERT INTO public.workforce_activity_events(
            event_id,event_type,actor_type,actor_id,worker_id,correlation_id,payload
        ) VALUES (
            'stage_forged_event_001','TASK_COMPLETED','WORKER','opus','opus',
            'stage_forged_event_corr_01','{}'::jsonb
        );
    EXCEPTION WHEN insufficient_privilege THEN
        v_rejected := true;
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Worker role unexpectedly forged an activity event';
    END IF;
END;
$event_acl$;
RESET SESSION AUTHORIZATION;

-- Even a superuser-originated arbitrary ORM result update has no authenticated
-- worker lease proof and must be rejected by the BEFORE UPDATE trigger.
DO $forged_result$
DECLARE
    v_rejected boolean := false;
BEGIN
    BEGIN
        UPDATE public.workforce_tasks
           SET status='COMPLETED', result_payload='{"source":"FORGED"}'::jsonb
         WHERE task_id='stage_writer_haiku_01';
    EXCEPTION WHEN raise_exception THEN
        v_rejected := SQLERRM LIKE '%authenticated worker completion%';
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Unauthenticated result payload publication was not rejected';
    END IF;
END;
$forged_result$;

-- Retention authorization must use the authenticated session identity, not
-- current_user (which is the SECURITY DEFINER function owner inside the trigger).
DO $purge_roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_workforce_maint') THEN
        CREATE ROLE sireader_workforce_maint
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_purge_test_untrusted') THEN
        CREATE ROLE sireader_purge_test_untrusted
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
    END IF;
END;
$purge_roles$;

GRANT USAGE ON SCHEMA public TO sireader_workforce_maint, sireader_purge_test_untrusted;
GRANT SELECT ON public.workforce_tasks TO sireader_workforce_maint, sireader_purge_test_untrusted;
GRANT UPDATE (input_payload,result_payload,payload_purged_at)
    ON public.workforce_tasks TO sireader_workforce_maint, sireader_purge_test_untrusted;
CREATE POLICY staging_worker_purge_test_select_v1
    ON public.workforce_tasks FOR SELECT
    TO sireader_workforce_maint, sireader_purge_test_untrusted
    USING (task_id IN ('stage_purge_untrusted_01','stage_purge_maintenance_01'));
CREATE POLICY staging_worker_purge_test_update_v1
    ON public.workforce_tasks FOR UPDATE
    TO sireader_workforce_maint, sireader_purge_test_untrusted
    USING (task_id IN ('stage_purge_untrusted_01','stage_purge_maintenance_01'))
    WITH CHECK (task_id IN ('stage_purge_untrusted_01','stage_purge_maintenance_01'));

SET SESSION AUTHORIZATION sireader_purge_test_untrusted;
DO $untrusted_purge$
DECLARE
    v_rejected boolean := false;
BEGIN
    BEGIN
        UPDATE public.workforce_tasks
           SET input_payload='{}'::jsonb,result_payload=NULL,payload_purged_at=clock_timestamp()
         WHERE task_id='stage_purge_untrusted_01';
    EXCEPTION WHEN raise_exception THEN
        v_rejected := SQLERRM LIKE '%authorized retention boundary%';
    END;
    IF NOT v_rejected THEN
        RAISE EXCEPTION 'Non-maintenance identity cleared an expired task result';
    END IF;
END;
$untrusted_purge$;
RESET SESSION AUTHORIZATION;

SET SESSION AUTHORIZATION sireader_workforce_maint;
DO $authorized_purge$
DECLARE
    v_rows integer;
BEGIN
    UPDATE public.workforce_tasks
       SET input_payload='{}'::jsonb,result_payload=NULL,payload_purged_at=clock_timestamp()
     WHERE task_id='stage_purge_maintenance_01';
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'Authorized maintenance identity did not purge exactly one expired task payload';
    END IF;
END;
$authorized_purge$;
RESET SESSION AUTHORIZATION;

DO $purge_audit$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_tasks
        WHERE task_id='stage_purge_untrusted_01' AND result_payload IS NOT NULL
          AND payload_purged_at IS NULL
    ) THEN
        RAISE EXCEPTION 'Rejected retention attempt changed the task payload';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.workforce_tasks
        WHERE task_id='stage_purge_maintenance_01' AND result_payload IS NULL
          AND payload_purged_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Authorized maintenance payload cleanup was not recorded';
    END IF;
END;
$purge_audit$;

SELECT 'STAGING_WORKER_COMMAND_LAYER_TESTS_PASS' AS acceptance_result;
ROLLBACK;
