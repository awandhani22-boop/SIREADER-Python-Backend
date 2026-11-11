-- SIREADER Phase 7.4 operator/review/maintenance authority acceptance.
-- STAGING ONLY: this script deliberately uses one transaction and ROLLBACK.
-- Do not remove or relax the target guard; never run this against Supabase LIVE.
\set ON_ERROR_STOP on
BEGIN;

DO $guard$
BEGIN
    IF current_database() <> 'sireader_stage'
       OR inet_server_addr() IS DISTINCT FROM inet '127.0.0.1'
       OR inet_server_port() <> 55439
       OR session_user <> 'postgres' THEN
        RAISE EXCEPTION 'Operator/review/maintenance acceptance is restricted to sireader_stage on 127.0.0.1:55439 as postgres';
    END IF;
    IF to_regprocedure('sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text)') IS NULL
       OR to_regprocedure('sireader_workforce_private.review_adjudicate_v1(text,text,text,jsonb,text,text,text)') IS NULL
       OR to_regprocedure('sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)') IS NULL THEN
        RAISE EXCEPTION 'Operator/review/maintenance staging migration has not been applied';
    END IF;
END;
$guard$;

-- 1. Command API: enqueue, idempotency, cancellation, scheduling and safe retry.
SET SESSION AUTHORIZATION sireader_workforce_cmd;
SELECT (sireader_workforce_private.command_enqueue_task_v1(
    'QUICK_RESEARCH','Phase 7.4 command acceptance','quick_research','haiku',
    '{"sample":"synthetic only"}'::jsonb,50,1,'staging-cmd-enqueue-accept-01'
)->'outcome'->>'task_id') AS enqueue_task_id \gset
SELECT (sireader_workforce_private.command_enqueue_task_v1(
    'QUICK_RESEARCH','Phase 7.4 command acceptance','quick_research','haiku',
    '{"sample":"synthetic only"}'::jsonb,50,1,'staging-cmd-enqueue-accept-01'
)->>'replayed') AS enqueue_replay \gset
SELECT 1 / CASE WHEN :'enqueue_replay'::boolean IS TRUE THEN 1 ELSE 0 END AS command_idempotency_assert;
SELECT (sireader_workforce_private.command_cancel_task_v1(
    :'enqueue_task_id','staging-cmd-cancel-accept-01'
)->'outcome'->>'status') AS cancel_status \gset
SELECT (sireader_workforce_private.command_set_worker_scheduling_v1(
    'haiku',false,'staging-cmd-pause-accept-01'
)->'outcome'->>'schedule_state') AS paused_state \gset
SELECT (sireader_workforce_private.command_set_worker_scheduling_v1(
    'haiku',true,'staging-cmd-resume-accept-01'
)->'outcome'->>'schedule_state') AS resumed_state \gset
RESET SESSION AUTHORIZATION;

SELECT 1 / CASE WHEN :'cancel_status'='CANCELLED' AND :'paused_state'='PAUSED' AND :'resumed_state'='ACCEPTING' THEN 1 ELSE 0 END AS command_state_assert;

-- A request that has crossed the provider boundary must never be blindly cancelled/retried.
INSERT INTO public.workforce_tasks(
    task_id,idempotency_key,correlation_id,schema_version,task_type,title,
    required_capability,requested_worker_id,assigned_worker_id,priority,status,
    input_payload,attempts,max_attempts,not_before,created_at,updated_at,
    lease_token,lease_owner,lease_expires_at
) VALUES (
    'stg_cmd_unknown_accept_01','stg_cmd_unknown_idem_01','stg_cmd_unknown_corr_01',1,
    'QUICK_RESEARCH','Unknown provider fixture','quick_research','haiku','haiku',50,
    'RUNNING','{"synthetic":true}'::jsonb,1,2,now(),now(),now(),
    'stg_cmd_unknown_lease_01','haiku-staging',now()+interval '1 minute'
);
INSERT INTO public.workforce_task_attempts(
    attempt_id,task_id,attempt_number,worker_id,lease_token,status,
    provider_request_id,provider_started_at,started_at,created_at
) VALUES (
    'stg_cmd_unknown_attempt_01','stg_cmd_unknown_accept_01',1,'haiku',
    'stg_cmd_unknown_lease_01','PROVIDER_REQUESTED','stg_provider_uncertain_01',
    now(),now(),now()
);
SET SESSION AUTHORIZATION sireader_workforce_cmd;
SELECT (sireader_workforce_private.command_cancel_task_v1(
    'stg_cmd_unknown_accept_01','staging-cmd-cancel-unknown-01'
)->'outcome'->>'status') AS unknown_cancel_status \gset
RESET SESSION AUTHORIZATION;
SELECT 1 / CASE WHEN :'unknown_cancel_status'='WAITING_REVIEW'
  AND (SELECT status FROM public.workforce_task_attempts WHERE attempt_id='stg_cmd_unknown_attempt_01')='UNKNOWN'
  THEN 1 ELSE 0 END AS unknown_outcome_assert;

-- An idempotency key cannot be replayed across a different command type.
INSERT INTO public.workforce_tasks(
    task_id,idempotency_key,correlation_id,schema_version,task_type,title,
    required_capability,requested_worker_id,priority,status,input_payload,
    attempts,max_attempts,not_before,created_at,updated_at
) VALUES (
    'stg_cmd_idem_conflict_01','stg_cmd_idem_conflict_src_01','stg_cmd_idem_conflict_corr_01',1,
    'QUICK_RESEARCH','Idempotency conflict fixture','quick_research','haiku',50,'FAILED',
    '{}'::jsonb,0,1,now(),now(),now()
);
SET SESSION AUTHORIZATION sireader_workforce_cmd;
SELECT sireader_workforce_private.command_cancel_task_v1(
    'stg_cmd_idem_conflict_01','staging-cross-command-idem-01'
);
DO $idempotency$
BEGIN
    BEGIN
        PERFORM sireader_workforce_private.command_retry_task_v1(
            'stg_cmd_idem_conflict_01','staging-cross-command-idem-01'
        );
        RAISE EXCEPTION 'CROSS_COMMAND_IDEMPOTENCY_NOT_REJECTED';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM <> 'IDEMPOTENCY_CONFLICT' THEN RAISE; END IF;
    END;
END;
$idempotency$;
RESET SESSION AUTHORIZATION;

-- Safe retry is allowed only when the prior provider outcome is known not sent.
INSERT INTO public.workforce_tasks(
    task_id,idempotency_key,correlation_id,schema_version,task_type,title,
    required_capability,requested_worker_id,assigned_worker_id,priority,status,
    input_payload,attempts,max_attempts,not_before,created_at,updated_at,failure_code
) VALUES (
    'stg_cmd_retry_src_accept_01','stg_cmd_retry_src_idem_01','stg_cmd_retry_corr_01',1,
    'QUICK_RESEARCH','Retry source fixture','quick_research','haiku','haiku',50,'FAILED',
    '{"synthetic":true}'::jsonb,1,2,now(),now(),now(),'SYNTHETIC_NOT_SENT'
);
INSERT INTO public.workforce_task_attempts(
    attempt_id,task_id,attempt_number,worker_id,lease_token,status,
    started_at,finished_at,error_code,created_at
) VALUES (
    'stg_cmd_retry_attempt_accept_01','stg_cmd_retry_src_accept_01',1,'haiku',
    'stg_cmd_retry_lease_accept_01','FAILED',now()-interval '1 minute',now(),
    'SYNTHETIC_NOT_SENT',now()
);
SET SESSION AUTHORIZATION sireader_workforce_cmd;
SELECT (sireader_workforce_private.command_retry_task_v1(
    'stg_cmd_retry_src_accept_01','staging-cmd-retry-accept-01'
)->'outcome'->>'task_id') AS retry_task_id \gset
RESET SESSION AUTHORIZATION;
SELECT 1 / CASE WHEN EXISTS (
    SELECT 1 FROM public.workforce_tasks
    WHERE task_id=:'retry_task_id' AND status='QUEUED'
      AND related_task_id='stg_cmd_retry_src_accept_01'
) THEN 1 ELSE 0 END AS safe_retry_assert;

-- 2. Review operator: APPROVED releases original, EDITED sanitizes, REJECTED keeps NULL.
DO $seed_reviews$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_output jsonb;
    v_hash text;
    v_i integer;
    v_task text;
    v_review text;
    v_attempt text;
BEGIN
    FOR v_i IN 1..3 LOOP
        v_task := 'stg_review_accept_task_' || v_i::text;
        v_review := 'rev_stg_review_accept_' || v_i::text || '_01';
        v_attempt := 'stg_review_accept_attempt_' || v_i::text;
        v_output := jsonb_build_object('summary','original review output ' || v_i);
        v_hash := encode(extensions.digest(
            convert_to(sireader_workforce_private.canonical_json_v1(v_output),'UTF8'),
            'sha256'
        ),'hex');
        INSERT INTO public.workforce_tasks(
            task_id,idempotency_key,correlation_id,schema_version,task_type,title,
            required_capability,requested_worker_id,assigned_worker_id,priority,status,
            input_payload,attempts,max_attempts,not_before,created_at,updated_at,
            completed_at,payload_expires_at
        ) VALUES (
            v_task,'idem_'||v_task,'corr_'||v_task,1,'QUICK_RESEARCH',
            'Review acceptance fixture '||v_i,'quick_research','opus','opus',50,
            'COMPLETED','{}'::jsonb,1,1,v_now,v_now,v_now,v_now,v_now+interval '30 days'
        );
        INSERT INTO public.workforce_task_attempts(
            attempt_id,task_id,attempt_number,worker_id,lease_token,status,
            started_at,finished_at,created_at
        ) VALUES (
            v_attempt,v_task,1,'opus','lease_'||v_attempt,'COMPLETED',
            v_now-interval '1 minute',v_now,v_now
        );
        INSERT INTO public.workforce_task_reviews(
            review_id,task_id,attempt_id,status,original_output_sha256,
            review_expires_at,created_at,updated_at
        ) VALUES (
            v_review,v_task,v_attempt,'PENDING',v_hash,
            v_now+interval '1 day',v_now,v_now
        );
        INSERT INTO public.workforce_task_review_payloads(
            review_id,original_output,created_at,updated_at
        ) VALUES (v_review,v_output,v_now,v_now);
    END LOOP;
END;
$seed_reviews$;
SELECT original_output_sha256::text AS approve_hash FROM public.workforce_task_reviews
WHERE review_id='rev_stg_review_accept_1_01' \gset
SELECT original_output_sha256::text AS edit_hash FROM public.workforce_task_reviews
WHERE review_id='rev_stg_review_accept_2_01' \gset
SELECT original_output_sha256::text AS reject_hash FROM public.workforce_task_reviews
WHERE review_id='rev_stg_review_accept_3_01' \gset

SET SESSION AUTHORIZATION sireader_workforce_review;
SELECT (sireader_workforce_private.review_get_pending_v1(
    'rev_stg_review_accept_1_01')->>'review_id') AS fetched_pending_review \gset
SELECT (sireader_workforce_private.review_adjudicate_v1(
    'rev_stg_review_accept_1_01','APPROVED',:'approve_hash',NULL,NULL,
    'approved in staging','staging-review-approve-accept-01'
)->'outcome'->>'decision') AS approve_decision \gset
SELECT (sireader_workforce_private.review_adjudicate_v1(
    'rev_stg_review_accept_2_01','EDITED',:'edit_hash',
    '{"summary":"edited staging output","api_token":"synthetic-redaction-probe"}'::jsonb,
    NULL,'edited in staging','staging-review-edit-accept-01'
)->'outcome'->>'decision') AS edit_decision \gset
SELECT (sireader_workforce_private.review_adjudicate_v1(
    'rev_stg_review_accept_3_01','REJECTED',:'reject_hash',NULL,
    'TEST_REJECTED','rejected in staging','staging-review-reject-accept-01'
)->'outcome'->>'decision') AS reject_decision \gset
RESET SESSION AUTHORIZATION;

SELECT 1 / CASE WHEN
    :'fetched_pending_review'='rev_stg_review_accept_1_01'
    AND :'approve_decision'='APPROVED' AND :'edit_decision'='EDITED' AND :'reject_decision'='REJECTED'
    AND (SELECT result_payload->>'summary' FROM public.workforce_tasks WHERE task_id='stg_review_accept_task_1')='original review output 1'
    AND (SELECT result_payload->>'api_token' FROM public.workforce_tasks WHERE task_id='stg_review_accept_task_2')='[REDACTED]'
    AND (SELECT result_payload FROM public.workforce_tasks WHERE task_id='stg_review_accept_task_3') IS NULL
    AND (SELECT adjudicated_by FROM public.workforce_task_reviews WHERE review_id='rev_stg_review_accept_1_01')='local_operator'
  THEN 1 ELSE 0 END AS review_release_assert;

-- 3. Maintenance: lease recovery, UNKNOWN quarantine, deadline/review expiry, payload purge.
DO $seed_maintenance$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_hash text;
BEGIN
    INSERT INTO public.workforce_tasks(
        task_id,idempotency_key,correlation_id,schema_version,task_type,title,
        required_capability,requested_worker_id,assigned_worker_id,priority,status,
        input_payload,result_payload,attempts,max_attempts,not_before,created_at,updated_at,
        lease_token,lease_owner,lease_expires_at
    ) VALUES
    ('stg_maint_retry_accept_01','stg_maint_retry_idem_accept_01','stg_maint_retry_corr_accept_01',1,
     'QUICK_RESEARCH','Expired pre-provider lease','quick_research','haiku','haiku',50,'CLAIMED',
     '{"synthetic":true}'::jsonb,NULL,1,3,v_now-interval '2 minutes',v_now-interval '2 minutes',
     v_now-interval '2 minutes','stg_maint_retry_lease_accept_01','haiku-staging',v_now-interval '1 minute'),
    ('stg_maint_unknown_accept_01','stg_maint_unknown_idem_accept_01','stg_maint_unknown_corr_accept_01',1,
     'QUICK_RESEARCH','Expired after provider request','quick_research','haiku','haiku',50,'RUNNING',
     '{"synthetic":true}'::jsonb,NULL,1,3,v_now-interval '2 minutes',v_now-interval '2 minutes',
     v_now-interval '2 minutes','stg_maint_unknown_lease_accept_01','haiku-staging',v_now-interval '1 minute'),
    ('stg_maint_deadline_accept_01','stg_maint_deadline_idem_accept_01','stg_maint_deadline_corr_accept_01',1,
     'QUICK_RESEARCH','Expired queued deadline','quick_research','haiku',NULL,50,'QUEUED',
     '{"synthetic":true}'::jsonb,NULL,0,1,v_now-interval '2 days',v_now-interval '2 days',
     v_now-interval '2 days',NULL,NULL,NULL),
    ('stg_maint_payload_accept_01','stg_maint_payload_idem_accept_01','stg_maint_payload_corr_accept_01',1,
     'QUICK_RESEARCH','Expired completed payload','quick_research','haiku','haiku',50,'COMPLETED',
     '{"synthetic":"payload"}'::jsonb,'{"summary":"expired"}'::jsonb,1,1,
     v_now-interval '2 days',v_now-interval '2 days',v_now-interval '2 days',
     NULL,NULL,NULL);
    UPDATE public.workforce_tasks SET deadline_at=v_now-interval '1 minute'
    WHERE task_id='stg_maint_deadline_accept_01';
    UPDATE public.workforce_tasks SET completed_at=v_now-interval '2 days',
        payload_expires_at=v_now-interval '1 day'
    WHERE task_id='stg_maint_payload_accept_01';

    INSERT INTO public.workforce_task_attempts(
        attempt_id,task_id,attempt_number,worker_id,lease_token,status,
        provider_request_id,provider_started_at,started_at,created_at
    ) VALUES
    ('stg_maint_retry_attempt_accept_01','stg_maint_retry_accept_01',1,'haiku',
     'stg_maint_retry_lease_accept_01','STARTED',NULL,NULL,v_now-interval '2 minutes',v_now-interval '2 minutes'),
    ('stg_maint_unknown_attempt_accept_01','stg_maint_unknown_accept_01',1,'haiku',
     'stg_maint_unknown_lease_accept_01','PROVIDER_REQUESTED','stg_provider_uncertain_accept_01',
     v_now-interval '2 minutes',v_now-interval '2 minutes',v_now-interval '2 minutes'),
    ('stg_maint_payload_attempt_accept_01','stg_maint_payload_accept_01',1,'haiku',
     'stg_maint_payload_lease_accept_01','COMPLETED',NULL,NULL,v_now-interval '2 days',v_now-interval '2 days');

    v_hash := encode(extensions.digest(convert_to(sireader_workforce_private.canonical_json_v1(
        '{"summary":"retained review fixture"}'::jsonb),'UTF8'),'sha256'),'hex');
    INSERT INTO public.workforce_task_reviews(
        review_id,task_id,attempt_id,status,original_output_sha256,
        review_expires_at,created_at,updated_at
    ) VALUES (
        'rev_stg_maint_review_accept_01','stg_maint_payload_accept_01',
        'stg_maint_payload_attempt_accept_01','PENDING',v_hash,
        v_now-interval '1 day',v_now-interval '2 days',v_now-interval '2 days'
    );
    INSERT INTO public.workforce_task_review_payloads(
        review_id,original_output,created_at,updated_at
    ) VALUES (
        'rev_stg_maint_review_accept_01','{"summary":"retained review fixture"}'::jsonb,
        v_now-interval '2 days',v_now-interval '2 days'
    );
END;
$seed_maintenance$;

SET SESSION AUTHORIZATION sireader_workforce_maint;
SELECT sireader_workforce_private.maintenance_run_once_v1(20,NULL) AS maintenance_counts \gset
SELECT 1 / CASE WHEN
    (:'maintenance_counts'::jsonb->>'leases_recovered_requeued')::integer=1
    AND (:'maintenance_counts'::jsonb->>'leases_recovered_waiting_review')::integer=1
    AND (:'maintenance_counts'::jsonb->>'deadline_tasks_expired')::integer=1
    AND (:'maintenance_counts'::jsonb->>'stale_reviews_expired')::integer=1
    AND (:'maintenance_counts'::jsonb->>'task_payloads_purged')::integer=1
    AND (:'maintenance_counts'::jsonb->>'review_payloads_purged')::integer=1
  THEN 1 ELSE 0 END AS maintenance_count_assert;
RESET SESSION AUTHORIZATION;

DO $assert$
BEGIN
    IF (SELECT status FROM public.workforce_tasks WHERE task_id='stg_maint_retry_accept_01') <> 'RETRY_WAIT' THEN
        RAISE EXCEPTION 'Expired pre-provider lease was not safely requeued';
    END IF;
    IF (SELECT status FROM public.workforce_tasks WHERE task_id='stg_maint_unknown_accept_01') <> 'WAITING_REVIEW' THEN
        RAISE EXCEPTION 'Expired post-provider lease was not quarantined';
    END IF;
    IF (SELECT status FROM public.workforce_task_attempts WHERE attempt_id='stg_maint_unknown_attempt_accept_01') <> 'UNKNOWN' THEN
        RAISE EXCEPTION 'Post-provider attempt did not preserve UNKNOWN';
    END IF;
    IF (SELECT status FROM public.workforce_tasks WHERE task_id='stg_maint_deadline_accept_01') <> 'DEAD_LETTERED' THEN
        RAISE EXCEPTION 'Expired queued deadline was not dead-lettered';
    END IF;
    IF (SELECT payload_purged_at IS NULL FROM public.workforce_tasks
        WHERE task_id='stg_maint_payload_accept_01') THEN
        RAISE EXCEPTION 'Expired task payload was not purged';
    END IF;
    IF (SELECT status FROM public.workforce_task_reviews
        WHERE review_id='rev_stg_maint_review_accept_01') <> 'EXPIRED' THEN
        RAISE EXCEPTION 'Stale review was not expired';
    END IF;
    IF (SELECT payload_purged_at IS NULL OR original_output IS NOT NULL
        FROM public.workforce_task_review_payloads
        WHERE review_id='rev_stg_maint_review_accept_01') THEN
        RAISE EXCEPTION 'Expired review payload was not purged';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM sireader_workforce_private.authority_maintenance_audit
        WHERE database_role='sireader_workforce_maint' AND batch_size=20
    ) THEN
        RAISE EXCEPTION 'Maintenance run was not independently audited';
    END IF;
END;
$assert$;

-- 4. Hard cap and least-privilege negative tests.
SET SESSION AUTHORIZATION sireader_workforce_maint;
DO $bounds$
BEGIN
    BEGIN
        PERFORM sireader_workforce_private.maintenance_run_once_v1(501,NULL);
        RAISE EXCEPTION 'MAINTENANCE_BATCH_LIMIT_NOT_ENFORCED';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM='MAINTENANCE_BATCH_LIMIT_NOT_ENFORCED' THEN RAISE; END IF;
    END;
    BEGIN
        PERFORM sireader_workforce_private.maintenance_run_once_v1(10,clock_timestamp()+interval '10 years');
        RAISE EXCEPTION 'MAINTENANCE_CALLER_TIME_NOT_REJECTED';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM='MAINTENANCE_CALLER_TIME_NOT_REJECTED' THEN RAISE; END IF;
    END;
END;
$bounds$;
RESET SESSION AUTHORIZATION;

SET SESSION AUTHORIZATION sireader_workforce_cmd;
DO $negative$
BEGIN
    BEGIN PERFORM 1 FROM public.workforce_tasks; RAISE EXCEPTION 'COMMAND_DIRECT_TASK_SELECT_NOT_DENIED'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN PERFORM sireader_workforce_private.review_get_pending_v1(NULL); RAISE EXCEPTION 'COMMAND_CAN_CALL_REVIEW'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN PERFORM sireader_workforce_private.maintenance_run_once_v1(10,NULL); RAISE EXCEPTION 'COMMAND_CAN_CALL_MAINTENANCE'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END;
$negative$;
RESET SESSION AUTHORIZATION;

SET SESSION AUTHORIZATION sireader_workforce_review;
DO $negative$
BEGIN
    BEGIN PERFORM 1 FROM public.workforce_task_reviews; RAISE EXCEPTION 'REVIEW_DIRECT_SELECT_NOT_DENIED'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN UPDATE public.workforce_task_reviews SET notes='forged' WHERE review_id='none'; RAISE EXCEPTION 'REVIEW_DIRECT_UPDATE_NOT_DENIED'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN PERFORM sireader_workforce_private.command_cancel_task_v1('stg_missing_task_01','staging-review-command-accept-01'); RAISE EXCEPTION 'REVIEW_CAN_CALL_COMMAND'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN PERFORM sireader_workforce_private.maintenance_run_once_v1(10,NULL); RAISE EXCEPTION 'REVIEW_CAN_CALL_MAINTENANCE'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END;
$negative$;
RESET SESSION AUTHORIZATION;

SET SESSION AUTHORIZATION sireader_workforce_maint;
DO $negative$
BEGIN
    BEGIN PERFORM 1 FROM public.workforce_tasks; RAISE EXCEPTION 'MAINT_DIRECT_SELECT_NOT_DENIED'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN PERFORM sireader_workforce_private.command_cancel_task_v1('stg_missing_task_01','staging-maint-command-accept-01'); RAISE EXCEPTION 'MAINT_CAN_CALL_COMMAND'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
    BEGIN PERFORM sireader_workforce_private.review_get_pending_v1(NULL); RAISE EXCEPTION 'MAINT_CAN_CALL_REVIEW'; EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END;
$negative$;
RESET SESSION AUTHORIZATION;

DO $acl$
BEGIN
    IF has_table_privilege('sireader_workforce_cmd','public.workforce_tasks','SELECT')
       OR has_table_privilege('sireader_workforce_cmd','public.workforce_tasks','INSERT')
       OR has_table_privilege('sireader_workforce_review','public.workforce_task_reviews','SELECT')
       OR has_table_privilege('sireader_workforce_review','public.workforce_task_reviews','UPDATE')
       OR has_table_privilege('sireader_workforce_maint','public.workforce_tasks','UPDATE') THEN
        RAISE EXCEPTION 'Authority role has direct workforce base-table privileges';
    END IF;
    IF has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.review_get_pending_v1(text)','EXECUTE')
       OR has_function_privilege('sireader_workforce_review','sireader_workforce_private.command_cancel_task_v1(text,text)','EXECUTE')
       OR has_function_privilege('sireader_workforce_maint','sireader_workforce_private.command_cancel_task_v1(text,text)','EXECUTE')
       OR has_function_privilege('sireader_api_ro','sireader_workforce_private.command_cancel_task_v1(text,text)','EXECUTE')
       OR has_function_privilege('sireader_workforce_cmd','sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)','EXECUTE') THEN
        RAISE EXCEPTION 'An authority role has an out-of-scope function EXECUTE grant';
    END IF;
    IF pg_has_role('sireader_workforce_cmd','sireader_workforce_fn_owner','MEMBER')
       OR pg_has_role('sireader_workforce_review','sireader_workforce_fn_owner','MEMBER')
       OR pg_has_role('sireader_workforce_maint','sireader_workforce_fn_owner','MEMBER') THEN
        RAISE EXCEPTION 'Authority role unexpectedly inherits function-owner membership';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) acl
        WHERE n.nspname='sireader_workforce_private'
          AND p.proname IN (
            'command_enqueue_task_v1','command_cancel_task_v1','command_retry_task_v1',
            'command_set_worker_scheduling_v1','review_get_pending_v1',
            'review_adjudicate_v1','maintenance_run_once_v1'
          )
          AND acl.grantee=0 AND acl.privilege_type='EXECUTE'
    ) THEN RAISE EXCEPTION 'PUBLIC has EXECUTE on an authority entry function'; END IF;
END;
$acl$;

SELECT 'STAGING_OPERATOR_REVIEW_MAINTENANCE_COMMAND_LAYER_TESTS_PASS' AS acceptance_result;
ROLLBACK;
