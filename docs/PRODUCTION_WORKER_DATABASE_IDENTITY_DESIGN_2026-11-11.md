# Production Worker Database Identity & Writer Boundary Design

**Date:** 2026-11-11 (Asia/Jakarta)
**Status:** DESIGN COMPLETE FOR REVIEW — NO PRODUCTION ROLE OR MIGRATION CREATED/APPLIED

## 1. Decision

The generic worker runtime must not use the core application `DATABASE_URL` or the Supabase `postgres.<project-ref>` login. A separate `WORKER_DATABASE_URL` now exists in backend configuration and is required for every non-MOCK or live-provider-enabled worker process.

The only permitted login identity is:

- Direct connection username: `sireader_worker_runtime`
- Supavisor pooler-style username: `sireader_worker_runtime.<project-ref>`
- Remote connections must specify `sslmode=require`, `verify-ca`, or `verify-full`.

If the URL is absent, invalid, uses another database role, or lacks remote TLS, the worker fails closed **before heartbeat, task claim, or provider dispatch**. MOCK-only execution retains the prior local session factory for compatibility. This fallback is not permitted when the live-provider flag is enabled.

This is application-side boundary validation, not a substitute for database authentication, grants, RLS, triggers, or the production staging acceptance below.

## 2. Code delivered in this update

- `app/config.py`: optional `WORKER_DATABASE_URL` setting, empty by default.
- `app/workers/database.py`: URL/role/TLS validation and an independently bounded SQLAlchemy pool (2 base connections + 1 overflow).
- `app/workers/runtime.py`: worker session factory is now isolated from the core global engine; checks its configuration before the first heartbeat/claim and before starting a foreground process.
- `.env.example`: documents the new variable without adding a live credential.
- `tests/workers/test_worker_database.py`: role, TLS, scheme, MOCK fallback, and secret-redaction cases.
- `tests/workers/test_runtime_guards.py` and `test_worker_runtime.py`: reject missing/broad worker DB identities before worker side effects; mocked LIVE-provider fixture uses only a synthetic loopback URL and FakeSession.

The real backend `.env` has **not** been edited. `WORKER_DATABASE_URL` remains unconfigured.

## 3. Current sanitized database topology

The current effective settings were classified without displaying any URL, password, host, or project reference:

| Setting | Current target class | Database | Identity class |
|---|---|---|---|
| `DATABASE_URL` | Remote / pooler | `postgres` | broad `postgres.*` |
| `SIREADER_API_DATABASE_URL` | Remote / pooler | `postgres` | `sireader_api_ro.*` |
| `SIREADER_WORKFORCE_COMMAND_DATABASE_URL` | Loopback | `sireader_disposable` | `sireader_workforce_cmd` |
| `SIREADER_WORKFORCE_MAINTENANCE_DATABASE_URL` | Not configured | — | — |
| `WORKER_DATABASE_URL` | Not configured | — | — |

The older `UNIFIED_WORKSPACE_INTEGRATION_2026-11-11.md` and `README_PETUNJUK.md` statements that the effective Read-Only API URL points to the local disposable database are historical and superseded by this runtime configuration audit. Do not print or paste connection strings when reconciling them.

## 4. Why a production writer migration is not yet safe to create

The current worker repository is an ORM CRUD workflow, not a small set of existing stored procedures. It needs narrowly scoped access to:

- Read worker definitions, worker capabilities, assignments, and task metadata.
- Write its own process heartbeat.
- Lock/claim eligible tasks, update leases and task lifecycle state.
- Insert attempt rows and update attempt state / provider request attribution.
- Append audit/activity events and consume the event identity sequence.
- Invoke the global provider slot gate and preserve permanent `UNKNOWN` quarantine.
- For eligible sensitive tasks, create a PENDING review metadata row and quarantined review-payload row.
- Release a normal, non-review result or preserve the review quarantine boundary.

A broad table-level `UPDATE` grant would allow much more than the worker's state machine requires. Column-level grants alone do not prove row ownership or legal lifecycle transitions. RLS policies, trigger-enforced transitions, and the application lease checks must be tested together.

The production workforce migrations already present in `supabase/migrations/` are read-model migrations. They intentionally grant no worker writer authority. The only provider-gate migration currently present under `migrations/` is guarded to `sireader_test`, uses a local test runtime identity, and must not be deployed as-is.

## 5. Required production role contract

The future database role must be created only on an isolated PG17/Supabase-equivalent staging clone after the migration design is accepted. The login must be:

- LOGIN, but NOT SUPERUSER / CREATEDB / CREATEROLE / REPLICATION / BYPASSRLS.
- NOINHERIT, and not a member of any other role.
- No schema CREATE, no migration ownership, no ownership of workforce tables/views, no grants on Telegram, legacy AI pipeline, approval, market-watch, trade, broker, or MT5/HFM objects.
- Connected over TLS when remote.
- Used only by the generic worker runtime through `WORKER_DATABASE_URL`.

A static SQL/RLS design must restrict worker identity/capability/task-type to the general-purpose allowlist (Haiku, Sonnet, Opus and their approved non-trading capabilities). Decision Engineer and Market Monitor must not gain provider dispatch or order/position authority.

The worker should not receive command API, human-review adjudication, or maintenance authority. Those are distinct trust boundaries.

## 6. Maintenance must remain separate

**Important current design debt:** `run_worker_once` still calls lease recovery, deadline expiry, stale-review expiry, and task/review payload purging on its periodic maintenance interval. The repository also has a separate one-shot `app.workers.maintenance` command with its own local-only identity, RLS policy set, and safety checks.

Before provisioning the production worker login, choose and test one of these designs:

1. Remove maintenance writes from the worker polling path and run them only through a separately authenticated, scheduled production maintenance identity with narrowly bounded operations; or
2. Replace them with reviewed, parameter-bounded database procedures whose execution grants are isolated from the ordinary worker login.

Do not grant the worker login the broad maintenance permissions just to make the existing polling loop work. The existing `2026_11_10_workforce_maintenance_role_v1.sql` and rollback are strictly local loopback `sireader_disposable` artifacts, not production migrations.

## 7. Provider gate and trusted result release

- Preserve the one global provider request limit and durable `PROVIDER_REQUESTED` / `UNKNOWN` occupancy. UNKNOWN must never be released by TTL or silently retried.
- The local test function `public.sireader_provider_slot_available_v1(integer)` is test-only, currently owned by `sireader_view_owner`, and explicitly denies PUBLIC, read-only API, and Command API execution. A production successor needs a dedicated migration, strict EXECUTE revokes/grants, fixed `search_path`, owner review, and staging concurrency/ACL tests.
- The review read-model migration intentionally has no worker writes. A future writer boundary must prove PENDING-only review creation, immutable original hash/output, write-once EDITED output, quarantine until approved/edited, non-resurrection after purge, and no worker ability to impersonate the human reviewer.
- A broad UPDATE on `workforce_tasks.result_payload` is not acceptable. Normal completion and reviewed output release must be separated and enforced by database triggers/functions. Positive and negative database tests are mandatory.

## 8. Staging acceptance matrix before production approval

Run against a fresh PG17 staging database restored from the production backup snapshot, never against Supabase LIVE during design:

1. Migrations apply idempotently in timestamp order and rollback in the documented reverse order.
2. Worker role can heartbeat, claim, start, renew, reserve one global provider slot, and append only valid worker audit events.
3. Worker role cannot write worker registry definitions/capabilities, command audit, review adjudications, core AI/Telegram/trade tables, or arbitrary task rows.
4. Worker role cannot assign itself a different worker identity or use another worker's lease token.
5. Role cannot read task/review payload outside its permitted in-flight completion path.
6. A rejected, missing, expired, or mismatched lease cannot update task or attempt.
7. Concurrency tests across Haiku/Sonnet/Opus yield exactly one provider slot; UNKNOWN remains blocking.
8. Normal completion writes only the expected task result. Review-required completion keeps task result NULL and inserts a quarantined review/payload.
9. Worker cannot approve/edit/reject reviews or impersonate `local_operator`.
10. Maintenance role can only expire/reap/purge eligible rows and cannot dispatch work or call a provider.
11. Read-only API remains SELECT-only on the 14 approved security-barrier views; it cannot execute worker gate or write functions.
12. Full backend tests, PostgreSQL privileges/RLS, API startup audit, endpoint smoke tests, rollback/forward, and restore rehearsal pass.

Capture query plans/latency for queue claim and RLS predicates on representative staging data. No blanket grants, no public function execution, and no unreviewed Data API exposure.

## 9. Verification in this update

- Focused DB-identity + runtime tests: **27 passed** after updating the existing mocked live-runtime fixture.
- Full backend root suite on a fresh disposable PostgreSQL 16.6 cluster: **690 passed, 1 skipped, 0 failed**.
- This full run included the PostgreSQL migration, rollback/forward, ACL/RLS, UNKNOWN-quarantine, and global concurrency integration tests.
- Python compileall: PASS.
- Backend `git diff --check`: PASS (Git printed only line-ending conversion warnings).
- Test server stopped; port 55444 closed; disposable cluster/data directory removed.
- Persistent runtime config remains OFF/MOCK, worker live disabled, budgets zero, and DRY_RUN. No worker process, model inference, Supabase mutation, Telegram, MT5/HFM, or trading action occurred.

## 10. Decision

**Code-level live-worker DB boundary: IMPLEMENTED and TESTED.**
**Production worker role/migration and maintenance split: NOT IMPLEMENTED / NOT DEPLOYED.**
**Production writer/command/review authority: NOT APPROVED.**
**Limited LIVE worker rollout: BLOCKED.**
**SIREADER Virtual Office v1.0: NOT RELEASED.**

This is the safe stopping point for this turn. A production role/migration cannot responsibly be created from table/ORM names alone; it requires the above staged permission design, database-enforced invariants, staging acceptance, and separate owner approval before any production DDL.


## Final test and URL-target guard correction — 2026-11-11

The MOCK fallback is now explicitly limited to live-provider-OFF and a core `DATABASE_URL` that is loopback and names `sireader_disposable`, `sireader_test`, or the unit-test-only DB. Thus the current remote/pooler core URL cannot be reused by a MOCK worker either.

The dedicated worker URL additionally rejects URL fragments, missing DB names, multiple `sslmode` values, and all query parameters except `sslmode` (including libpq host/hostaddr/service overrides). Remote URLs require an approved TLS mode. A 10-second connection timeout is set in addition to bounded pool settings.

Final focused worker tests: **42 passed**. Final full backend on fresh disposable PostgreSQL 16.6: **702 passed, 1 skipped, 0 failed**. Python compileall/diff check PASS. Temporary cluster stopped/removed; port 55448 closed. No production URL/role/secret or service setting was changed.


## Runtime / maintenance trust-boundary correction — 2026-11-11

The ordinary worker polling path no longer imports or invokes any of the five maintenance operations. `run_worker_once` now performs heartbeat, claim, execution, lease renewal, and task completion/failure only. Lease recovery, deadline expiry, stale-review expiry, and both payload-retention purges remain in the one-shot `app.workers.maintenance` command, which retains its independent URL/identity validation and dedicated engine.

This is an application-level separation only. Production maintenance is still blocked: the current maintenance URL is unset and the maintenance module still explicitly accepts only local loopback `sireader_disposable` with `sireader_workforce_maint`. Do not configure a production maintenance URL or remove the local-only guard until a separate production maintenance role and parameter-bounded database boundary have been designed and staged.

Regression verification after this change: focused runtime/review suite **60 passed, 17 skipped**. Full PostgreSQL-backed suite was not rerun in this update because no local PostgreSQL service or `initdb/pg_ctl/psql` binaries were available on the workstation at verification time. Previous full PG16 suite evidence (702 passed, 1 skipped) predates this small runtime separation change and must not be represented as a post-change full-suite result.


## Post-separation verification update — 2026-11-11

After removing the obsolete runtime fixture dependency on `_last_maintenance_at`, the full backend test suite was rerun without a PostgreSQL test service configured: **657 passed, 47 skipped, 0 failed**. The skipped tests include database-backed PostgreSQL integration/migration/security checks because `SIREADER_TEST_POSTGRES_URL` was intentionally unset and no local PostgreSQL service/binaries were available. Python compileall and `git diff --check` both passed. This is not a substitute for the PG16/PG17 migration, ACL, RLS, concurrency, and rollback rehearsal.

## Superseding worker identity implementation update ? 2026-11-11

The earlier shared sireader_worker_runtime login described in this design was the initial application-side URL guard, not the final staging identity model. The newer isolated PG17 worker command layer uses seven separate NOLOGIN/NOINHERIT worker permission roles and derives the allowed worker identity from session_user. A future production LOGIN or pooler identity must inherit only its corresponding worker permission role; no production LOGIN, password, or membership was created in the staging exercise.

See WORKER_DATABASE_COMMAND_LAYER_STAGING_ACCEPTANCE_2026-11-11.md. Production role provisioning and its final independent security review remain open.
