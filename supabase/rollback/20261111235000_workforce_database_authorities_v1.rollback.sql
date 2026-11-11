-- SIREADER workforce authority rollback v1.
-- Removes only the Phase 7 worker/command/review/maintenance authority layer.
-- Preserves the Phase 4 workforce schema, data tables, RLS, and 14 read-only API views.
-- Intentionally refuses rollback after any worker operational data exists.
BEGIN;

DO $guard$
DECLARE
    v_tables integer;
    v_views integer;
    v_sessions integer;
BEGIN
    IF current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Workforce authority rollback must be applied by the postgres migration operator';
    END IF;
    IF pg_catalog.to_regnamespace('sireader_workforce_private') IS NULL
       OR pg_catalog.to_regprocedure('sireader_workforce_private.worker_get_contract_v1(text)') IS NULL
       OR pg_catalog.to_regprocedure('sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text)') IS NULL THEN
        RAISE EXCEPTION 'Expected complete workforce authority layer is not installed';
    END IF;
    SELECT count(*) INTO v_tables
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname LIKE 'workforce_%'
      AND c.relkind IN ('r','p') AND c.relrowsecurity;
    SELECT count(*) INTO v_views
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname LIKE 'v_api_%' AND c.relkind='v'
      AND EXISTS (SELECT 1 FROM pg_catalog.pg_options_to_table(c.reloptions)
                  WHERE option_name='security_barrier' AND option_value='true');
    IF v_tables <> 9 OR v_views <> 14 THEN
        RAISE EXCEPTION 'Rollback requires nine RLS workforce base tables and fourteen security-barrier API views; found %, %',v_tables,v_views;
    END IF;
    IF EXISTS (SELECT 1 FROM public.workforce_tasks)
       OR EXISTS (SELECT 1 FROM public.workforce_task_attempts)
       OR EXISTS (SELECT 1 FROM public.workforce_task_reviews)
       OR EXISTS (SELECT 1 FROM public.workforce_task_review_payloads)
       OR EXISTS (SELECT 1 FROM public.workforce_heartbeats)
       OR EXISTS (SELECT 1 FROM public.workforce_activity_events) THEN
        RAISE EXCEPTION 'Rollback refused: workforce operational data exists. Use a forward repair/recovery plan instead.';
    END IF;
    SELECT count(*) INTO v_sessions
    FROM pg_catalog.pg_stat_activity
    WHERE usename IN (
        'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
        'sireader_worker_opus','sireader_worker_decision_engineer',
        'sireader_worker_market_monitor','sireader_worker_data_archivist',
        'sireader_workforce_cmd','sireader_workforce_review','sireader_workforce_maint'
    );
    IF v_sessions <> 0 THEN
        RAISE EXCEPTION 'Rollback refused: % workforce LOGIN sessions are still connected',v_sessions;
    END IF;
END;
$guard$;

DO $policies$
DECLARE v_policy record;
BEGIN
    FOR v_policy IN
        SELECT n.nspname AS schema_name,c.relname AS table_name,p.polname AS policy_name
        FROM pg_catalog.pg_policy p
        JOIN pg_catalog.pg_class c ON c.oid=p.polrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname LIKE 'workforce_%'
          AND EXISTS (
              SELECT 1 FROM pg_catalog.unnest(p.polroles) role_oid
              JOIN pg_catalog.pg_roles r ON r.oid=role_oid
              WHERE r.rolname IN (
                  'sireader_workforce_fn_owner',
                  'sireader_worker_supervisor','sireader_worker_haiku',
                  'sireader_worker_sonnet','sireader_worker_opus',
                  'sireader_worker_decision_engineer','sireader_worker_market_monitor',
                  'sireader_worker_data_archivist'
              )
          )
    LOOP
        EXECUTE pg_catalog.format(
            'DROP POLICY %I ON %I.%I',
            v_policy.policy_name,v_policy.schema_name,v_policy.table_name
        );
    END LOOP;
END;
$policies$;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM sireader_workforce_fn_owner;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM sireader_workforce_fn_owner;
REVOKE ALL PRIVILEGES ON SCHEMA public, extensions FROM sireader_workforce_fn_owner;
DROP SCHEMA sireader_workforce_private CASCADE;

DO $roles$
DECLARE v_role text;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'sireader_worker_supervisor','sireader_worker_haiku','sireader_worker_sonnet',
        'sireader_worker_opus','sireader_worker_decision_engineer',
        'sireader_worker_market_monitor','sireader_worker_data_archivist',
        'sireader_workforce_cmd','sireader_workforce_review','sireader_workforce_maint',
        'sireader_workforce_fn_owner'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=v_role) THEN
            EXECUTE pg_catalog.format('ALTER ROLE %I RESET ALL',v_role);
        END IF;
    END LOOP;
END;
$roles$;

DROP ROLE
    sireader_worker_supervisor,
    sireader_worker_haiku,
    sireader_worker_sonnet,
    sireader_worker_opus,
    sireader_worker_decision_engineer,
    sireader_worker_market_monitor,
    sireader_worker_data_archivist,
    sireader_workforce_cmd,
    sireader_workforce_review,
    sireader_workforce_maint,
    sireader_workforce_fn_owner;

DO $verify$
DECLARE v_tables integer; v_views integer; v_rls integer;
BEGIN
    SELECT count(*),count(*) FILTER (WHERE c.relrowsecurity)
    INTO v_tables,v_rls
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname LIKE 'workforce_%' AND c.relkind IN ('r','p');
    SELECT count(*) INTO v_views
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relname LIKE 'v_api_%' AND c.relkind='v'
      AND EXISTS (SELECT 1 FROM pg_catalog.pg_options_to_table(c.reloptions)
                  WHERE option_name='security_barrier' AND option_value='true');
    IF v_tables <> 9 OR v_rls <> 9 OR v_views <> 14
       OR pg_catalog.to_regnamespace('sireader_workforce_private') IS NOT NULL THEN
        RAISE EXCEPTION 'Rollback did not preserve the read-only workforce baseline or remove private authority schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname LIKE 'sireader_worker_%'
           OR rolname IN (
                'sireader_workforce_fn_owner','sireader_workforce_cmd',
                'sireader_workforce_review','sireader_workforce_maint'
           )
    ) THEN
        RAISE EXCEPTION 'Rollback left a workforce authority role behind';
    END IF;
END;
$verify$;

COMMIT;
