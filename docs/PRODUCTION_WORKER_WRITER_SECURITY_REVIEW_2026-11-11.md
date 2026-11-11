# SIREADER — Production Worker Writer Security Review

Date: 2026-11-11 (Asia/Jakarta)
Status: **STATIC REVIEW COMPLETE — WRITER DESIGN NOT APPROVED, NO PRODUCTION DDL**

## Executive decision

The application-side database identity guard is useful and tested, but it is not yet a production database authorization boundary. The current worker repository performs direct ORM reads and writes across workforce tables. The production Supabase migrations deliberately grant no writer role. A production sireader_worker_runtime login must not be provisioned from broad table grants or from the local test-only migrations.

**Decision: HOLD production writer provisioning and LIVE rollout.**

## Verified code path and data operations

| Operation | Objects / effects | Security implication |
|---|---|---|
| Worker contract load | Read workforce_workers, workforce_worker_capabilities and capability definitions | Worker can load registry metadata; it must not edit the canonical roster/capability allowlist |
| Heartbeat | Insert/update workforce_heartbeats | A shared DB login cannot independently prove which worker process is writing a worker_id |
| Claim | Lock worker row; read capability links and eligible queue; update task assignment/status/attempt count/lease; insert attempt and activity event | Broad UPDATE permits forged assignments, lease changes and invalid state transitions unless DB checks ownership and legal transitions |
| Start/renew | Lock task and attempt by lease token; update task/attempt state and lease expiry; append event | DB must bind active lease token to exact task, attempt, assigned worker and lease owner |
| Provider reservation | Call provider-slot gate; update attempt provider request fields; append event | EXECUTE must be granted only to runtime function path; UNKNOWN occupancy must remain permanent until explicit reconciliation |
| Completion | Update task status, result payload, payload expiry and clear lease; update attempt status/billing; append event | Result release must be lease-bound and one-write, not merely guarded by task status |
| Review quarantine | Insert PENDING review metadata and original output payload; leave task result NULL | Inserts must be bound to exact completed attempt/current lease; worker must never adjudicate |
| Failure | Update task/attempt status, error and expiry; clear lease; append event | DB must enforce transitions and preserve UNKNOWN after provider request |
| Maintenance | Recover expired leases, expire deadlines/reviews, purge task/review payloads | Must not run under worker runtime identity; now removed from polling path and isolated in one-shot maintenance module |

## Critical findings

### CRITICAL — Direct ORM writes are not a safe production writer contract

The runtime uses direct SQLAlchemy ORM updates, not a small set of database command procedures. A production role with broad UPDATE on workforce_tasks or workforce_task_attempts could change fields outside the intended worker state machine. Column-level grants reduce the surface but do not prove that a write belongs to the current lease or represents a legal transition.

**Required remediation:** before production, either replace state-changing repository calls with narrowly scoped database functions that validate lease and state transition, or implement and test equivalent trigger/RLS enforcement for every write path. Do not solve permission errors by granting table-wide UPDATE.

### CRITICAL — Current result-release trigger is not sufficient as the only worker authorization check

The local sireader_guard_workforce_result_release_v1 trigger protects one-write result publication and prevents a review-required result from bypassing an APPROVED/EDITED review. Its normal non-review path allows a non-command writer to publish when there is no review row and the task is COMPLETED. The trigger does not itself prove that the writer owns the task's previous active lease or that the associated attempt belongs to the assigned worker.

**Required remediation:** normal completion must be performed through a DB boundary that verifies the pre-update RUNNING task, old non-null lease token, matching task attempt, worker assignment, lease owner and valid attempt state inside the same transaction. A worker must not publish a result for an unrelated already-completed task. Add negative tests for wrong task, wrong worker, wrong attempt, stale token, expired lease, replay and duplicate completion.

### HIGH — Review creation needs a transaction-scoped completion proof

The review metadata/payload triggers protect PENDING-only creation, immutable original output/hash, write-once adjusted output, terminal-state immutability and retention purge. Those are valuable integrity controls, but the generic insert boundary must also prove that the review belongs to the exact attempt currently being completed. A generic INSERT grant alone is not proof of lease ownership.

**Required remediation:** make review creation part of the same narrow completion function/transaction. Worker may request PENDING quarantine creation only; it cannot set adjudication fields, change review status, write adjusted output, approve, reject or impersonate local_operator.

### HIGH — Shared worker login cannot attest per-worker identity by itself

The runtime accepts a worker_id argument and loads that worker's contract. If every process shares one database login, PostgreSQL sees one role, not an independently authenticated Haiku/Sonnet/Opus process. A compromised process with that login may attempt to claim work under another worker_id unless the DB boundary restricts it.

**Required decision:** either accept this as an explicitly documented application-trust boundary with tightly scoped service deployment and tested worker allowlists, or provision distinct database identities/credentials per worker class and bind each DB identity to an immutable worker_id. Do not claim that a single shared login provides database-enforced per-worker identity.

### HIGH — Event insertion needs actor integrity

The repository appends events with worker/system/operator actor fields. Append-only triggers protect existing events from mutation, but the writer boundary must also prevent a worker from inserting arbitrary actor types or impersonating an operator/system actor.

**Required remediation:** DB function/trigger must bind worker actor_id to authenticated worker identity, limit worker-allowed event types, and reserve SYSTEM/OPERATOR events to their respective trusted paths.

### HIGH — Maintenance separation is now implemented in application code, not production infrastructure

The worker polling path no longer imports or runs lease recovery, deadline/review expiry or payload purges. The one-shot maintenance module still has a strict local-only guard for loopback sireader_disposable and role sireader_workforce_maint; no production maintenance URL or role exists.

**Required remediation:** design a separate production maintenance identity and bounded operations. Maintenance must not have provider dispatch, task claim, worker registry write, review adjudication or command authority. Keep the local-only guard intact until a production design is staged and explicitly approved.

## Required role and access model before staging

1. **Worker runtime:** LOGIN, NOINHERIT, no superuser/createdb/createrole/replication/BYPASSRLS; no role memberships; no schema CREATE; no ownership; no access to Telegram, AI pipeline, approval, market-watch, trading, broker or MT5/HFM objects. Prefer EXECUTE on a reviewed set of worker functions rather than direct table writes.
2. **Maintenance:** separate LOGIN and connection secret; bounded expiry/recovery/purge functions only; no provider/claim/review adjudication.
3. **Command API:** separate role; cannot claim worker tasks or execute provider gate; task cancellation and operator commands only through its own validated command functions.
4. **Review operator:** separate trusted identity; can adjudicate only PENDING reviews and release only exact approved/edited output; cannot create worker attempts or call providers.
5. **Read-only API:** retain SELECT-only access to the 14 approved security-barrier views; no base-table access, no function execution for writer/maintenance/command paths.
6. **View owner:** NOLOGIN, non-superuser, non-BYPASSRLS, minimal column grants, no runtime use.

All new functions must use a fixed safe search_path, fully qualify referenced objects, revoke default EXECUTE from PUBLIC/anon/authenticated/service_role unless specifically required, and grant only to the exact intended role. Prefer SECURITY INVOKER. If SECURITY DEFINER is demonstrably required, isolate it in a non-exposed schema, use a tightly controlled non-login owner, and test effective privileges and RLS behavior.

## Mandatory staging acceptance

Staging must be a fresh PostgreSQL 17 production-like restore, not the local test DB and not Supabase LIVE. Apply the complete migration sequence in order, test rollback/forward, and execute the following negative cases under each real role:

- worker cannot update worker definitions/capabilities or change its identity;
- worker cannot select arbitrary unassigned task/review payloads;
- worker cannot claim a task assigned to another worker or use another attempt's lease;
- worker cannot renew, start, fail or complete with missing, expired, stale, mismatched or replayed lease token;
- worker cannot alter terminal tasks/attempts or append forbidden actor/event types;
- worker cannot publish an unrelated completed task result;
- worker cannot approve/edit/reject/expire a review or release quarantined output;
- worker cannot call maintenance or command functions;
- maintenance cannot claim/dispatch/provider-call or adjudicate reviews;
- command role cannot invoke provider gate or normal worker completion;
- read-only API cannot execute any writer function or access base tables;
- exactly one global provider slot can be acquired across concurrent workers;
- UNKNOWN remains occupied across restart, timeout and maintenance cycles;
- retention purge cannot resurrect task/review payloads;
- every role's effective grants, memberships, RLS policies and function EXECUTE ACLs are captured and reviewed.

Only after all positive and negative tests pass should a separately authorized operator approve a production migration window, verified restore point, rollback owner and limited LIVE rollout.

## Work completed in this update

- Removed all five maintenance operations from the ordinary worker runtime polling path.
- Added a regression test asserting the runtime module does not import maintenance operations.
- Focused runtime/review tests: 60 passed, 17 skipped.
- Full backend suite without PostgreSQL service: 657 passed, 47 skipped, 0 failed.
- Python compileall and git diff check: PASS.
- Runtime remains disabled; provider mode MOCK; live provider disabled; worker DB URL unset; paid daily budget zero; trading DRY_RUN; auto execution disabled.
- No Supabase LIVE mutation, production role creation, migration deployment, paid inference, Telegram, MT5/HFM or trading action.

## Final gate

**Static security review:** complete.
**Application maintenance split:** implemented and unit-tested.
**Production worker/maintenance roles and database functions:** not implemented or deployed.
**PG17 production-like staging rehearsal:** not available / not performed.
**Production cutover and limited LIVE rollout:** BLOCKED.
**SIREADER Virtual Office v1.0:** NOT RELEASED.


## Superseding staging evidence — 2026-11-11

A fresh PostgreSQL 17.11 rehearsal now confirms the existing read-only API/read-model baseline: 14/14 approved views owned by the NOLOGIN view owner, 14/14 security barriers, no API base-table or write access, 24/24 public base tables with RLS, 14 API endpoint probes passing, and explicit SelectorEventLoop SSE listener readiness. The full evidence is recorded in `SIREADER_PG17_STAGING_REHEARSAL_2026-11-11.md`.

This is not a writer acceptance test. It does not change the critical findings above: direct ORM mutation is still not a safe production writer contract; lease/attempt provenance and transaction-scoped review creation/release are not implemented as a complete production DB command boundary; shared worker identity and actor integrity still need a deliberate decision/enforcement. Production writer roles and LIVE rollout remain on HOLD.


## Implementation update — 2026-11-11

The worker command boundary has now been implemented and exercised in isolated local PG17 staging:

- Eight database command functions cover contract lookup, heartbeat, claim, start, lease renewal, provider reservation, completion/review quarantine, and failure.
- Seven worker permission roles are NOLOGIN/NOINHERIT and have no direct workforce base-table privileges.
- Database identity is bound through session_user; a pooler-style suffixed identity test confirms normalization and matching-role inheritance.
- Result publication, review provenance, event actor identity, provider slot reservation and lease ownership are enforced in PostgreSQL.
- Python runtime now calls app.workers.database_commands instead of repository ORM mutation functions for worker lifecycle operations.
- Python adapter integration was run against PG17 staging under the data_archivist identity and rolled back.
- Final backend regression: 666 passed, 47 skipped. Virtual Office: 422 passed; TypeScript and production build passed.

The following items remain release gates, not reasons to add duplicate command surfaces now:

1. Independent database/security review of the staging SQL and review-policy synchronization.
2. A production-ready Supabase migration/role provisioning plan, including dedicated per-worker logins or pooler identities and credential handling.
3. Production-specific ACL/RLS/function audit on a separately approved target.
4. Separate command API and maintenance role validation remain as their own boundaries.
5. Explicit production approval before any migration, role/login creation, runtime configuration change or LIVE enablement.

No production database, credential, URL, runtime flag, AI provider, Telegram listener, MT5/HFM connection, or trading execution was changed or enabled. The staging cluster is stopped after testing; its local files and certificates are retained.

## Superseding local staging remediation update — 2026-11-11

The critical findings in this static review are now addressed in the isolated worker-command staging implementation, but are not thereby approved for production deployment:

- Direct ORM worker lifecycle writes have been replaced in the runtime path by the narrow database command adapter for the staged worker operations.
- Completion is bound to the authenticated worker, exact task/attempt and active lease; review quarantine, event actor integrity and normal result release are exercised by the PG17.11 staging SQL acceptance.
- Provider reservation rejects trading-related capabilities and is executable only by Haiku, Sonnet and Opus worker permission roles.
- LIVE source/billing provenance is checked against the persisted provider reservation, worker class and USD billing fields. This is database consistency validation, not cryptographic attestation of provider response or invoice authenticity.
- SECURITY DEFINER retention authorization now uses session_user rather than current_user. Transactional tests demonstrate non-maintenance rejection and maintenance-only purge.
- The policy catalog and review decision function are synchronized with Python: 40 rules, 56 capability/task pairs and 1,120 deterministic comparisons pass.

See WORKER_DATABASE_SECURITY_REVIEW_CLOSEOUT_2026-11-11.md and WORKER_DATABASE_COMMAND_LAYER_STAGING_ACCEPTANCE_2026-11-11.md for complete evidence.

The following earlier findings remain valid release blockers for production: dedicated production login/pooler identities and credentials, separate bounded production maintenance operations, Command API and review-operator function boundaries, production-specific forward/rollback migrations, managed-Supabase-equivalent staging, production backup/recovery and change-window approval. No production DDL or credentials were created; WORKER_DATABASE_URL remains unset. Limited LIVE rollout remains NOT AUTHORIZED and v1.0 NOT RELEASED.
