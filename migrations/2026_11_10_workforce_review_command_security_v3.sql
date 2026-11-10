-- SIREADER Phase 5.2.2 — permit locking PENDING quarantined payloads without permitting edits.
-- SELECT ... FOR UPDATE is subject to UPDATE RLS policies in PostgreSQL.
BEGIN;
DO $guard$
DECLARE db_owner text;
BEGIN
  IF current_database() NOT IN ('sireader_disposable','sireader_test','sireader_ci') THEN
    RAISE EXCEPTION 'This migration is restricted to explicitly named disposable/test databases';
  END IF;
  IF current_database()='sireader_disposable' AND inet_server_addr() IS DISTINCT FROM '127.0.0.1'::inet THEN
    RAISE EXCEPTION 'The local disposable database must be reached through loopback';
  END IF;
  SELECT r.rolname INTO db_owner FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database();
  IF db_owner NOT IN ('sireader_dev','postgres') OR current_user NOT IN ('sireader_dev','postgres') THEN
    RAISE EXCEPTION 'This migration requires the disposable database owner';
  END IF;
  IF to_regclass('public.workforce_task_reviews') IS NULL OR to_regclass('public.workforce_task_review_payloads') IS NULL
     OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='sireader_workforce_cmd') THEN
    RAISE EXCEPTION 'Required review tables or command role are missing';
  END IF;
END;
$guard$;

-- The command role must lock the PENDING quarantined payload before deciding.
-- It may physically UPDATE adjusted_output only when the parent review is EDITED;
-- the WITH CHECK remains terminal-only and the existing payload trigger verifies
-- write-once/sanitized boundaries. No INSERT or DELETE policy is added.
DROP POLICY IF EXISTS workforce_cmd_review_payloads_update ON public.workforce_task_review_payloads;
CREATE POLICY workforce_cmd_review_payloads_update
ON public.workforce_task_review_payloads FOR UPDATE TO sireader_workforce_cmd
USING (
  EXISTS (
    SELECT 1 FROM public.workforce_task_reviews r
    WHERE r.review_id=workforce_task_review_payloads.review_id
      AND r.status IN ('PENDING','EDITED')
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM public.workforce_task_reviews r
    WHERE r.review_id=workforce_task_review_payloads.review_id
      AND r.status='EDITED'
  )
);
COMMIT;
