-- Local disposable-DB fixture for Phase 5.1 browser retry acceptance.
-- Never run against Supabase LIVE or any production database.
BEGIN;

INSERT INTO public.workforce_tasks (
    task_id, idempotency_key, correlation_id, schema_version,
    task_type, title, required_capability, requested_worker_id, priority,
    status, input_payload, attempts, max_attempts, not_before,
    failure_code, created_at, updated_at
)
VALUES (
    'phase5-retry-fixture-0001',
    'phase5-retry-fixture-key-0001',
    'phase5-retry-fixture-correlation-0001',
    1,
    'SUMMARIZATION',
    'Phase 5 local retry acceptance fixture',
    'summarization',
    'haiku',
    50,
    'FAILED',
    '{"instruction":"Local UI retry test only; no provider execution"}'::jsonb,
    1,
    2,
    now(),
    'PHASE5_LOCAL_FIXTURE_FAILED',
    now(),
    now()
)
ON CONFLICT (task_id) DO NOTHING;

INSERT INTO public.workforce_task_attempts (
    attempt_id, task_id, attempt_number, worker_id, lease_token,
    status, started_at, finished_at, created_at
)
VALUES (
    'phase5-retry-fixture-attempt-0001',
    'phase5-retry-fixture-0001',
    1,
    'haiku',
    'phase5-retry-fixture-lease-0001',
    'FAILED',
    now(),
    now(),
    now()
)
ON CONFLICT (attempt_id) DO NOTHING;

COMMIT;
