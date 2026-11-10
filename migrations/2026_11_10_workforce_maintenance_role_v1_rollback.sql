-- Roll back only the local disposable maintenance identity/policies.
BEGIN;

DO $guard$
BEGIN
    IF current_database() <> 'sireader_disposable'
       OR inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN
        RAISE EXCEPTION 'Maintenance role rollback is restricted to loopback sireader_disposable';
    END IF;
    IF current_user NOT IN ('postgres', 'sireader_dev')
       OR NOT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'Run this rollback only as the local disposable database superuser';
    END IF;
END;
$guard$;

DROP POLICY IF EXISTS workforce_maint_tasks_select ON public.workforce_tasks;
DROP POLICY IF EXISTS workforce_maint_tasks_update ON public.workforce_tasks;
DROP POLICY IF EXISTS workforce_maint_attempts_select ON public.workforce_task_attempts;
DROP POLICY IF EXISTS workforce_maint_attempts_update ON public.workforce_task_attempts;
DROP POLICY IF EXISTS workforce_maint_reviews_select ON public.workforce_task_reviews;
DROP POLICY IF EXISTS workforce_maint_reviews_update ON public.workforce_task_reviews;
DROP POLICY IF EXISTS workforce_maint_review_payloads_select ON public.workforce_task_review_payloads;
DROP POLICY IF EXISTS workforce_maint_review_payloads_update ON public.workforce_task_review_payloads;
DROP POLICY IF EXISTS workforce_maint_activity_insert ON public.workforce_activity_events;

REVOKE ALL ON public.workforce_tasks, public.workforce_task_attempts,
    public.workforce_task_reviews, public.workforce_task_review_payloads,
    public.workforce_activity_events
    FROM sireader_workforce_maint;
REVOKE ALL ON SEQUENCE public.workforce_activity_events_event_seq_seq
    FROM sireader_workforce_maint;
REVOKE USAGE ON SCHEMA public FROM sireader_workforce_maint;
REVOKE CONNECT ON DATABASE sireader_disposable FROM sireader_workforce_maint;
DROP OWNED BY sireader_workforce_maint;
DROP ROLE IF EXISTS sireader_workforce_maint;

COMMIT;
