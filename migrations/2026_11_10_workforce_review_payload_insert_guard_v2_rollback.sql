-- Rollback v2 INSERT guard to the UPDATE/DELETE trigger installed by workforce_reviews_v1.
BEGIN;

DROP TRIGGER IF EXISTS trg_workforce_task_review_payloads_guard ON public.workforce_task_review_payloads;
CREATE TRIGGER trg_workforce_task_review_payloads_guard
BEFORE UPDATE OR DELETE ON public.workforce_task_review_payloads
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_payloads_v1();

COMMIT;
