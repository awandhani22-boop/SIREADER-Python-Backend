# SIREADER — Worker Database Command Contract v1

Date: 2026-11-11
Status: DESIGN CONTRACT — NOT DEPLOYABLE SQL
Scope: workforce task runtime, provider reservation, quarantine creation, maintenance, command API and trusted review release.

## 1. Decision

Production worker and maintenance identities will not receive direct INSERT/UPDATE/DELETE privileges on workforce base tables. State changes must go through narrowly scoped PostgreSQL functions. Read access must be limited to the minimum required by each operation. The existing SQLAlchemy repository is the behavioral reference until the function contract has passed staging parity tests.

This document defines the target contract only. It is not a migration and must not be applied to Supabase.

## 2. Role model

| Role | Intended authority | Explicitly forbidden |
|---|---|---|
| sireader_worker_runtime | Worker heartbeat, claim eligible work, start/renew own lease, reserve one provider slot, complete/fail own lease, create PENDING quarantine | Worker roster edits, task enqueue/cancel, arbitrary task reads, review adjudication, maintenance, trading/Telegram/MT5 |
| sireader_workforce_maint | Recover expired leases, expire deadlines/reviews, purge expired payloads in bounded batches | Claim/start/dispatch/provider request, worker roster edit, review adjudication/release |
| sireader_command_api | Enqueue/cancel task, pause/resume worker through validated command functions | Worker claim/lease completion, provider reservation, review adjudication |
| sireader_review_operator | Read pending review, approve/edit/reject with exact hash and authorized operator identity | Claim tasks, call provider, change worker roster, maintenance |
| sireader_api_ro | SELECT on approved security-barrier views only | Base table access and every write/maintenance function |
| sireader_view_owner | NOLOGIN view ownership and narrowly scoped base-table SELECT | Login, runtime use, write authority, BYPASSRLS |

All login roles must be NOINHERIT, non-superuser, non-BYPASSRLS, without CREATE on exposed schemas, without ownership, and without role memberships that imply broader privileges. Passwords are provisioned out-of-band and are not committed to repository files.

## 3. Worker command API

All functions below belong in a non-exposed schema such as sireader_workforce_private. Function signatures are logical contracts; exact PostgreSQL composite/JSON return types are to be finalized with the migration implementation. Every function uses a fixed safe search_path, fully qualified object names, bounded inputs, and explicit EXECUTE ACLs.

### 3.1 Heartbeat

sireader_workforce_private.worker_heartbeat_v1(
  p_worker_id text,
  p_instance_id text,
  p_process_state text,
  p_process_started_at timestamptz,
  p_heartbeat_at timestamptz,
  p_active_task_count integer,
  p_in_flight_provider_requests integer
) RETURNS heartbeat projection

Checks:
- authenticated database identity maps to the permitted worker identity (or, if a single shared identity is retained, the deployment trust limitation is explicitly accepted and enforced by a trusted worker gateway);
- worker exists, is enabled, and supplied fields are within bounds;
- timestamps cannot move backwards;
- heartbeat actor is always derived by the function, never caller-supplied.

### 3.2 Claim

sireader_workforce_private.worker_claim_next_v1(
  p_worker_id text,
  p_instance_id text,
  p_lease_seconds integer
) RETURNS one claimed task + attempt projection, or no row

Atomic transaction:
- lock worker row and verify enabled/accepting;
- lock/verify fresh READY heartbeat for exact worker + instance;
- derive capabilities from canonical enabled assignments;
- enforce max concurrency;
- select highest effective-priority eligible task with SKIP LOCKED;
- enforce requested_worker_id and capability;
- create unpredictable lease token and matching attempt;
- update task, insert attempt and append event with actor derived by function;
- return only the claimed task's minimum execution projection, including input payload only after successful claim.

Must not accept a caller-supplied task_id, assigned_worker_id, attempt_number, lease token, actor type or actor_id.

### 3.3 Start and renew

sireader_workforce_private.worker_start_v1(p_task_id text, p_lease_token text)
sireader_workforce_private.worker_renew_lease_v1(p_task_id text, p_lease_token text, p_lease_seconds integer)

Each function locks task and attempt in deterministic order and verifies:
- task is CLAIMED/RUNNING as appropriate;
- task lease token matches exact attempt token;
- attempt.task_id matches task;
- attempt.worker_id matches task.assigned_worker_id;
- lease_owner matches authenticated worker instance identity;
- lease has not expired;
- attempt status is a legal predecessor.

Renewal cannot revive an expired lease. Start/renew actor metadata is derived inside the database function.

### 3.4 Provider reservation

sireader_workforce_private.worker_reserve_provider_slot_v1(
  p_task_id text,
  p_lease_token text,
  p_provider_request_id text
) RETURNS attempt projection

Checks exact live lease, RUNNING task, STARTED attempt, provider policy, global one-slot gate and durable provider request reservation in one transaction. No second call may be made by a replay. UNKNOWN and provider-requested states stay occupied until an explicit reconciliation path. This function must be the only worker-reachable path to the provider gate.

### 3.5 Completion and review quarantine

sireader_workforce_private.worker_complete_task_v1(
  p_task_id text,
  p_lease_token text,
  p_result_payload jsonb,
  p_billing_currency text,
  p_cost_amount numeric,
  p_force_human_review boolean,
  p_review_trigger_reason text,
  p_policy_context jsonb
) RETURNS task completion projection

Single transaction:
- lock exact task + attempt;
- prove lease, owner, worker assignment, attempt linkage, expiry and legal state;
- validate payload size/shape and cost/billing fields;
- derive review eligibility server-side from canonical capability/task policy; caller cannot turn off mandatory review;
- if review is required: create PENDING metadata + immutable sanitized original payload + hash; task result remains NULL; task is completed with lease cleared;
- otherwise publish result exactly once;
- append event actor as the authenticated worker, with constrained event payload;
- commit all task/attempt/review/event writes atomically.

Worker role cannot write review status, adjudicator, adjusted output, adjudication timestamps, approval/rejection reasons, or invoke the review operator function.

### 3.6 Failure

sireader_workforce_private.worker_fail_task_v1(
  p_task_id text,
  p_lease_token text,
  p_error_code text,
  p_retryable boolean,
  p_outcome_unknown boolean,
  p_backoff_seconds integer
) RETURNS task projection

Verifies exact current lease/attempt and legal transition. If provider request was recorded or outcome is unknown, force WAITING_REVIEW/UNKNOWN and never retry automatically. All backoff and error fields are bounded; caller cannot choose terminal state directly.

## 4. Maintenance command API

Maintenance functions use only the separate sireader_workforce_maint identity and must be bounded:

- maintenance_recover_expired_leases_v1(p_batch_size, p_now) — batch 1..100; provider-requested attempt -> UNKNOWN + WAITING_REVIEW, never replay; pre-provider expired leases may retry only if policy/deadline/attempt count permit.
- maintenance_expire_deadlines_v1(p_batch_size, p_now) — queued/retry work only.
- maintenance_expire_stale_reviews_v1(p_batch_size, p_now) — PENDING -> EXPIRED only; no approval/release.
- maintenance_purge_task_payloads_v1(p_batch_size, p_now) — terminal/retention eligible rows only; preserve metadata and review quarantine.
- maintenance_purge_review_payloads_v1(p_batch_size, p_now) — terminal review and task retention eligibility only.

Batch size hard maximum 500, server-side bounded execution, deterministic ordering, SKIP LOCKED, audit event actor fixed to maintenance/system. Maintenance functions cannot call worker claim/provider functions. Do not expose arbitrary table names, SQL fragments, status values or actor identities as parameters.

## 5. Command and review APIs

Command API:
- enqueue_task_v1 — idempotency and task/capability validation in DB; actor derived from authenticated command role.
- cancel_task_v1 — provider-requested work becomes UNKNOWN/WAITING_REVIEW; otherwise cancel only legal active states.
- set_worker_accepting_tasks_v1 — allowlist of worker ids; append operator event. No worker definition or capability mutation.

Review operator:
- get_pending_review_v1 — only pending review projection and payload for authorized operator.
- adjudicate_review_v1 — decision APPROVED/EDITED/REJECTED only; require exact expected SHA-256, pending status, matching task/attempt and authenticated operator identity. EDITED requires sanitized adjusted output and hash. REJECTED requires reason code. Status update and result release occur atomically. Expiry remains maintenance-only and cannot release a result.
- review actor is derived from database identity; never accept arbitrary operator_id as authority.

## 6. Database enforcement and ACL requirements

- RLS remains enabled and deny-by-default on workforce base tables.
- Runtime roles get no direct base-table UPDATE/DELETE/INSERT.
- Prefer SECURITY INVOKER functions with narrowly scoped table privileges where practical.
- If SECURITY DEFINER is required, owner must be a NOLOGIN, non-superuser, non-BYPASSRLS role; function must be in a non-exposed schema; fixed search_path; fully qualified objects; no dynamic SQL from caller data; revoke EXECUTE from PUBLIC, anon, authenticated and service_role unless explicitly justified.
- Grant EXECUTE to exact role only. API read-only role receives no writer function EXECUTE.
- Trigger functions remain non-callable directly.
- Sequence usage is restricted to the internal owner/function path.
- Actor type/id and event type are derived/allowlisted server-side.
- Database constraints/triggers enforce legal transitions, attempt linkage, result write-once, review release and append-only events.
- No worker/maintenance role may set session_replication_role, disable triggers, alter policies, or change function definitions.

## 7. Migration sequence

M0 — Baseline inventory and backup:
- Capture exact production schema/migration version, role memberships, grants, RLS policies, function ACLs and view definitions.
- Verify restore point and owner; no schema changes.

M1 — Private schema and NOLOGIN owners:
- Create non-exposed private schema and function owner role.
- Harden default privileges and function search_path.
- No login roles yet.

M2 — Worker functions:
- Implement heartbeat, claim, start, renew, provider reservation, completion, failure.
- No direct worker table grants.
- Keep provider mode MOCK in staging until all security tests pass.

M3 — Maintenance functions:
- Add separate bounded maintenance procedures and role.
- Keep application maintenance local-only guard until staging has proven new role.
- Confirm maintenance cannot invoke worker/provider/review functions.

M4 — Command and review functions:
- Move enqueue/cancel/pause/resume and review adjudication to distinct identities.
- Preserve review release triggers and add lease-to-attempt provenance checks.

M5 — Application adapter:
- Replace ORM mutation code with function calls.
- Keep domain payload validation in Python and duplicate authoritative validation in DB.
- Read projections only from returned rows/approved views.
- Do not leave an ORM fallback for production writer identity.

M6 — ACL/RLS lockdown:
- Revoke direct table grants from API, runtime, maintenance, command and review logins.
- Grant exact function EXECUTE ACLs.
- Audit inherited memberships and default privileges.

M7 — PG17 staging rehearsal:
- Apply all migrations on a production-like isolated restore.
- Exercise rollback/forward and all role-specific positive/negative tests.
- Capture effective grants, policies and function ACLs.
- Test concurrent provider reservation and UNKNOWN persistence.

M8 — Production approval:
- Requires explicit approval, maintenance window, restore point, rollback operator, monitoring and bounded rollout.
- No automatic promotion from staging to production.

## 8. Mandatory negative tests

- Runtime cannot write worker/capability definitions or assignments.
- Runtime cannot read arbitrary unassigned task payloads or review payloads.
- Worker A cannot claim or mutate Worker B's task.
- Wrong task, wrong attempt, wrong instance, stale token, expired lease, replayed token and duplicate completion all fail.
- Caller cannot choose actor type/id or append operator/system event.
- Worker cannot mark review approved/edited/rejected/expired or release review payload.
- Worker cannot call maintenance, command or review functions.
- Maintenance cannot claim, start, reserve provider slot, complete tasks, enqueue, cancel or adjudicate.
- Command cannot call worker/provider/maintenance/review release functions.
- Review operator cannot claim tasks or call providers.
- Read-only API cannot read base tables or execute any mutation function.
- Provider slot concurrency test across >= 10 concurrent transactions yields exactly one acquisition.
- UNKNOWN remains occupied after process restart, lease expiry and all maintenance cycles.
- Review output can only be released once with exact matching hash and authenticated reviewer.
- Event actor and event type cannot be spoofed.
- Payload purge cannot erase PENDING review payload and cannot resurrect purged data.
- RLS, SECURITY DEFINER owner, search_path, PUBLIC ACL, role memberships and default privileges are asserted in SQL tests.

## 9. Compatibility and rollout rules

- Preserve current runtime contract and event semantics; add adapter-level parity tests against existing repository behavior.
- No LIVE provider call during migration or test rehearsal.
- Synthetic task fixtures only; no real Telegram messages, market feeds, broker data or trading signals in staging.
- Existing AI pipeline and trading switches remain OFF/DRY_RUN.
- Do not provision production role credentials until staging ACL tests pass.
- Do not apply local-only migrations to Supabase.
- This document is a design contract and must be reviewed before executable migration authoring.

## 10. Exit criteria

**Design:** contract complete; independent security review still required.
**SQL implementation:** NOT STARTED.
**Application adapter conversion:** NOT STARTED.
**PG17 staging:** BLOCKED by missing PostgreSQL runtime/tooling on workstation.
**Supabase production:** untouched.
**LIVE rollout:** BLOCKED.


## 11. PG17 read-model staging evidence — 2026-11-11

The current production read-model baseline and API read-only boundary now pass a fresh PostgreSQL 17.11 local staging rehearsal. Four existing read-model/security-barrier migrations applied; the 14-view startup audit and endpoint probes passed. Full evidence is in `docs/SIREADER_PG17_STAGING_REHEARSAL_2026-11-11.md`.

This does not implement any command contract in this document. SQL worker/maintenance/command/review function implementation remains NOT STARTED; application adapter conversion remains NOT STARTED; no production writer role exists. Keep the rollout blocked until the critical writer-security findings have a database-enforced implementation and positive/negative PG17 tests.

## Superseding staging implementation status ? 2026-11-11

The SQL implementation and application adapter are no longer NOT STARTED for the isolated staging boundary described by this contract. The implementation is located separately at backend/staging/2026_11_11_worker_database_command_layer_v1.sql with its transactional acceptance script and Python adapter smoke test. It was applied and tested only on loopback PostgreSQL 17.11 database sireader_stage. The eight worker command functions and seven NOLOGIN worker permission roles passed the staging acceptance recorded in WORKER_DATABASE_COMMAND_LAYER_STAGING_ACCEPTANCE_2026-11-11.md.

This supersedes only the design-time SQL/adapter status above. The staged SQL is deliberately target-guarded and is NOT a production migration. Production login provisioning, production-specific migration/rollback, independent security approval, and bounded LIVE authorization remain NOT APPROVED.

## Superseding Phase 7.4 implementation and review status — 2026-11-11

The earlier design-time M2/M5/M7 statements above are historical and are superseded for the isolated worker command boundary:

- The eight ordinary worker command functions and Python database command adapter are implemented in the staging-only SQL artifact and app/workers/database_commands.py.
- Seven NOLOGIN/NOINHERIT worker permission roles map database session identities to immutable worker IDs. Worker roles have no direct workforce base-table grants.
- PG17.11 isolated staging SQL acceptance and Python adapter integration passed; all synthetic task transactions were rolled back.
- Follow-up security remediations reject trading-related provider reservations, require persisted provider reservation and USD billing for LIVE provenance, validate worker profile/model/policy metadata, quarantine truncated LIVE outputs, and enforce maintenance purge identity using session_user.
- Python and PostgreSQL review-policy catalog/decision parity passed for 40 rules, 56 capability/task pairs and 1,120 deterministic decision comparisons.
- Effective staged ACL/RLS audit passed: all nine workforce tables have RLS enabled, worker permission roles have zero direct base-table privileges, SECURITY DEFINER functions use a fixed pg_catalog search_path, and PUBLIC EXECUTE is revoked.

The implementation remains a local staging acceptance artifact, not a deployable production migration. It does not implement or approve the full production maintenance, Command API, or review-operator database command surfaces. Production LOGIN/pooler identity creation, managed-Supabase-specific migration and rollback, backup/recovery approval, read-only API cutover and fresh limited-LIVE authorization remain open. The earlier architecture role contract remains the target for those separate trust boundaries.

Evidence and findings: backend/docs/WORKER_DATABASE_SECURITY_REVIEW_CLOSEOUT_2026-11-11.md and WORKER_DATABASE_COMMAND_LAYER_STAGING_ACCEPTANCE_2026-11-11.md.
