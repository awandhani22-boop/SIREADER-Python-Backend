-- Rollback v3 to EDITED-only UPDATE USING policy.
BEGIN;
DO $guard$
DECLARE db_owner text;
BEGIN
  IF current_database() NOT IN ('sireader_disposable','sireader_test','sireader_ci') THEN RAISE EXCEPTION 'Disposable/test DB only'; END IF;
  IF current_database()='sireader_disposable' AND inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN RAISE EXCEPTION 'Loopback only'; END IF;
  SELECT r.rolname INTO db_owner FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database();
  IF db_owner NOT IN ('sireader_dev','postgres') OR current_user NOT IN ('sireader_dev','postgres') THEN RAISE EXCEPTION 'Database owner required'; END IF;
END;
$guard$;
DROP POLICY IF EXISTS workforce_cmd_review_payloads_update ON public.workforce_task_review_payloads;
CREATE POLICY workforce_cmd_review_payloads_update
ON public.workforce_task_review_payloads FOR UPDATE TO sireader_workforce_cmd
USING (
  EXISTS (SELECT 1 FROM public.workforce_task_reviews r
          WHERE r.review_id=workforce_task_review_payloads.review_id AND r.status='EDITED')
)
WITH CHECK (
  EXISTS (SELECT 1 FROM public.workforce_task_reviews r
          WHERE r.review_id=workforce_task_review_payloads.review_id AND r.status='EDITED')
);
COMMIT;
