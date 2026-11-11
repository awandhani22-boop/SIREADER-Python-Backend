# Worker Database Command Layer — Staging Acceptance

Date: 2026-11-11
Scope: isolated local PostgreSQL 17.11 staging only.

## Result

The worker command SQL layer was installed and exercised on the restored local sireader_stage database. A separate Python adapter integration exercised the SQLAlchemy-to-PostgreSQL path under a worker identity. All test transactions containing synthetic tasks were rolled back.

This is staging acceptance for the worker command boundary. It is not production migration approval, production writer acceptance, or SIREADER v1.0 release approval.

## Implemented worker command surface

The staging-only SQL artifact is:

- backend/staging/2026_11_11_worker_database_command_layer_v1.sql

The corresponding acceptance tests are:

- backend/staging/test_worker_database_command_layer_v1.sql
- backend/staging/worker_database_adapter_smoke.py
- backend/tests/workers/test_database_commands_adapter.py

Database functions exposed to the seven worker identities:

1. worker_get_contract_v1
2. worker_heartbeat_v1
3. worker_claim_next_v1
4. worker_start_v1
5. worker_renew_lease_v1
6. worker_reserve_provider_slot_v1 — Haiku, Sonnet, Opus only
7. worker_complete_task_v1
8. worker_fail_task_v1

The Python adapter is app/workers/database_commands.py. app/workers/runtime.py now imports its worker operations through this adapter rather than importing mutation functions from app.workers.repository.

The adapter passes worker ID and instance ID to every lease-bound command. PostgreSQL derives the authoritative worker identity from session_user; caller-supplied worker IDs are checked against that identity. Task and attempt row locks are acquired inside the database command transaction.

## Identity and privilege boundary

Seven NOLOGIN, NOINHERIT, non-privileged worker permission roles are created for:

- supervisor
- haiku
- sonnet
- opus
- decision_engineer
- market_monitor
- data_archivist

Production login/password creation is intentionally absent from the staging artifact. A deployable login must be a dedicated per-worker identity and must inherit only its corresponding worker permission role. For a Supabase-style username suffix, the SQL identity mapping normalizes the first username segment and the pooler login inherits only the matching worker permission role. The staging acceptance includes a suffix-identity test.

Worker permission roles have no direct SELECT/INSERT/UPDATE privilege on workforce base tables. Their only mutation route is the granted command function surface. They have no superuser, create-role, create-database, replication, bypass-RLS, or role-membership privileges.

The sireader_workforce_fn_owner role is NOLOGIN, non-privileged and owns the security-definer command functions. Its table/schema grants are limited to the worker command operations. Worker functions use a fixed search_path.

## Enforced database behavior

- Heartbeats reject invalid process states, stale/reversed timestamps and out-of-range counters.
- Claim requires a fresh READY heartbeat, enabled/accepting worker, eligible capability/task pair, available concurrency, a queued task and a database-created lease token.
- Start, renew, provider reservation, completion and failure require the worker's exact task lease, matching attempt, instance owner and unexpired lease.
- Provider reservation is serialized by a transaction advisory lock. The staging contract permits one global in-flight/unknown provider request. An UNKNOWN provider outcome remains quarantined and prevents a second reservation.
- Only Haiku, Sonnet and Opus can reserve an AI provider slot.
- Completion binds publication to the previous active lease and exact completing attempt. Review creation occurs in the same transaction while the lease proof is still present.
- Review-required outputs are sanitized, hashed and placed in review payload storage; the task result remains NULL until the review release boundary.
- PII detected in task input/title or completion output forces review. The acceptance includes output-PII quarantine.
- Direct or unauthenticated result publication is rejected by a database trigger.
- Worker activity actor identity/type is checked by a database trigger. Direct worker event INSERT is not granted.
- A worker cannot claim another worker's identity or execute a provider command outside its worker allowlist.

The review policy rows mirror app/governance/review_policy.py version 2026.11.01; keep the Python catalog and database rules synchronized whenever that policy changes.

## Verification evidence

Staging verification performed on local PostgreSQL 17.11 at loopback port 55439:

- SQL migration applied transactionally.
- SQL acceptance test result: STAGING_WORKER_COMMAND_LAYER_TESTS_PASS.
- Five synthetic tasks exercised: Haiku, Sonnet, Opus, Data Archivist normal completion, Data Archivist output-PII quarantine.
- Provider global slot contention and UNKNOWN outcome tested.
- Cross-worker identity spoof tested and rejected.
- Direct worker table read/write and forged activity insert tested and rejected.
- Arbitrary ORM-style result publication tested and rejected.
- Pooler-style suffixed identity membership and normalization tested.
- Python adapter integration result: PYTHON_DATABASE_COMMAND_ADAPTER_PG17_PASS.
- Actual adapter path exercised: contract lookup, heartbeat, claim, start, complete.
- Python adapter unit tests: 7 passed.
- Worker database/runtime targeted regression: 52 passed.
- Full backend suite after runtime integration: 659 passed, 47 skipped. A final full rerun is recorded separately in the completion report.

Synthetic task writes in both SQL and Python integration tests were rolled back. No persistent test task was left in the staging database.

## Runtime integration status

The runtime adapter switch is in place. The opt-in runtime still has its existing guards:

- WORKER_RUNTIME_ENABLED=False
- WORKER_PROVIDER_MODE=MOCK
- WORKER_LIVE_PROVIDER_ENABLED=False
- WORKER_DATABASE_URL unset
- AI_PIPELINE_ENABLED=False
- Trading remains DRY_RUN
- Auto execution remains disabled

A live runtime cannot start without a dedicated per-worker database URL. No worker process was started.

## Explicitly out of scope / release gates

- No production Supabase SQL migration was executed.
- No production worker role/login/password was created or rotated.
- No production URL or environment configuration was changed.
- The existing command API remains on its separate dedicated command database identity, row-level security and idempotent audit path; it was not duplicated into worker functions.
- Maintenance remains a separate one-shot boundary and was not granted to worker identities.
- Independent SQL/security review and a production migration plan are still required before any production deployment.
- Production approval and any live worker enablement remain separate explicit gates.
- AI inference, Telegram ingestion, MT5/HFM and broker execution were not used.

The staging SQL file is deliberately guarded to refuse any target other than the local sireader_stage database on 127.0.0.1:55439 as postgres. It must not be copied into a production migration directory without a separate review and deployment plan.


## Final regression verification

After the adapter wiring, per-worker login validation, numeric cost binding and output-PII review check were in place:

- Full backend regression after provider credential-boundary hardening: **670 passed, 47 skipped, 0 failed**. The 47 skipped cases require a separately configured PostgreSQL test URL; isolated PG17 SQL/adapter acceptance is recorded above.
- Virtual Office regression: 422 passed, 0 failed.
- Virtual Office TypeScript check: passed.
- Virtual Office production build: passed.
- Python worker adapter focused suite: 52 passed.
- Staging SQL command acceptance: passed, including the pooler-style suffixed login test.
- Python-to-PG17 adapter integration: passed.

The production build reports a non-blocking large JavaScript chunk warning (1,511.12 kB minified; 405.35 kB gzip). No bundle-splitting refactor was added because it is unrelated to the worker database security boundary.
