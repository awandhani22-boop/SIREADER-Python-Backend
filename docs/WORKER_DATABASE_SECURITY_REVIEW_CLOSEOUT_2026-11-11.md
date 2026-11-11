# SIREADER Worker Database Security Review & Staging Closeout

Date: 2026-11-11 (Asia/Jakarta)
Scope: Phase 7.4 worker command boundary, Python policy parity, isolated PostgreSQL 17.11 staging
Decision: **STAGING SECURITY REVIEW PASS WITH REMEDIATIONS â€” PRODUCTION READINESS BLOCKED**

This is a documented second-pass technical review of the staged implementation. It is not an external penetration test, a managed-Supabase certification, or authorization to deploy production DDL.

## 1. Executive result

The worker database command layer is now exercised against an isolated PostgreSQL 17.11 restore. The review closed three concrete database-boundary gaps:

1. Provider-slot reservation now rejects every capability classified as trading-related, even if the caller is an otherwise approved AI worker.
2. Completion validates explicit result provenance, provider reservation/billing consistency, worker profile/model tier, policy metadata consistency, and mandatory quarantine for truncated LIVE output.
3. Result-payload retention now checks the authenticated session_user identity rather than current_user, which changes to the SECURITY DEFINER function owner inside a definer-trigger context.

Python and PostgreSQL human-review policy definitions were compared exhaustively for the current registry and passed. SQL command acceptance, Python adapter integration, backend regression, frontend regression, TypeScript and production build all passed.

**This closes the reviewed local staging scope only. It does not close production role provisioning, production migration/rollback, Supabase API cutover, backup/recovery approval, or bounded LIVE rollout. SIREADER Virtual Office v1.0 remains NOT RELEASED.**

## 2. Findings and remediation

### 2.1 Critical â€” trading capability could reach the provider slot

Before remediation, the provider reservation function restricted the caller to Haiku, Sonnet or Opus, but did not independently reject all trading-related task capabilities. Opus has a registered decision_evaluation capability, so a direct authenticated database caller could have asked the provider reservation boundary to process a task that Python governance classifies as trading-related.

**Remediation:** the staging function now rejects the canonical trading-related capability set before checking or taking the global provider slot. The acceptance test attempts an Opus DECISION_EVALUATION reservation and verifies the database rejects it.

### 2.2 High â€” caller-authored result provenance and policy metadata

Before remediation, completion accepted a caller-provided result source and a policy-context object whose shape was validated more strongly than its meaning. A database caller could have claimed LIVE without proving a prior provider reservation, supplied a tier inconsistent with its worker profile, or emitted contradictory policy metadata.

**Remediation:** the staging completion function now requires an explicit supported source; binds LIVE to a persisted PROVIDER_REQUESTED attempt, request ID, approved AI worker and USD billing fields; prevents unreserved completion from claiming LIVE or provider billing; restricts PYTHON and LIVE provenance to approved worker classes; and checks the policy version, reason/action combination, redaction count and fixed worker model tier. The completion event records the validated source, not a default-to-LIVE fallback.

**Trust boundary:** this validates database state and consistency of the payload submitted by the trusted runtime. It is not a cryptographic attestation that the provider actually returned those bytes, nor independent provider-invoice verification. The application/provider gateway remains part of the trusted computing base.

### 2.3 High â€” truncated LIVE output could bypass mandatory review

A provider response marked as truncated was not an independent database-level review trigger.

**Remediation:** when source=LIVE and output_truncated=true, the staging completion function forces review quarantine, leaves the task result unpublished, and records OUTPUT_TRUNCATED_REVIEW. A synthetic accepted test exercises this path without contacting any provider.

### 2.4 High â€” SECURITY DEFINER retention identity confusion

The result-release trigger is SECURITY DEFINER. Inside that trigger current_user is the function owner, not the authenticated database login. Allowing the function owner in a current_user allowlist therefore does not distinguish a maintenance session from an ordinary caller.

**Remediation:** the staging trigger now checks the normalized session_user identity for sireader_workforce_maint or the local staging postgres operator. A negative test confirms a non-maintenance identity cannot clear an expired result; a positive test confirms the maintenance identity can purge a fully expired payload. Test roles and policies exist only inside the acceptance transaction and are rolled back.

### 2.5 Policy synchronization â€” Python and SQL review decisions

Added staging/worker_review_policy_parity_pg17.py. The script refuses any target except TLS-enabled loopback sireader_stage on port 55439. It compares the exact 40-rule catalog (version, mode, sample percentage and salt), then compares Python and SQL decisions over 56 capability/task pairs and 20 deterministic task IDs.

Result: **1,120 decision comparisons passed**, catalog equality passed, and unknown capability behavior failed closed in both implementations.

## 3. Staging acceptance evidence

Target identity was verified for every staging connection: database sireader_stage, PostgreSQL 17.11, server address 127.0.0.1, port 55439, authenticated session_user postgres, and TLS 1.3. Before applying the reviewed staging SQL, a local custom-format backup was created at C:\SIREADER-STAGING\sireader_stage_before_worker_security_review_2026-11-11.dump. The backup remains local and is not committed.

Verified results:

| Check | Result |
|---|---:|
| Python/PostgreSQL review-rule catalog | 40 rules equal |
| Capability/task policy pairs | 56 pairs |
| Deterministic Python/SQL decisions | 1,120 comparisons passed |
| Unknown capability | Fail closed |
| Worker command SQL transaction | STAGING_WORKER_COMMAND_LAYER_TESTS_PASS |
| Python adapter / PostgreSQL integration | PYTHON_DATABASE_COMMAND_ADAPTER_PG17_PASS |
| Adapter unit tests | 7 passed |
| Full backend suite | 670 passed, 47 skipped, 0 failed |
| Python compileall | Passed |
| Virtual Office suite | 422 passed, 0 failed |
| TypeScript check | Passed |
| Virtual Office production build | Passed |

The SQL acceptance exercised synthetic claim/start/provider-reservation/completion and failure paths, UNKNOWN provider occupancy, cross-worker identity rejection, review quarantine and output sanitization, truncated LIVE quarantine, trading-capability provider denial, fake LIVE provenance rejection, worker model-tier mismatch, direct event-write denial, unauthenticated result publication denial, and maintenance-only retention. SQL test fixtures were executed inside one transaction and rolled back.

The Python adapter smoke ran contract lookup, heartbeat, claim, start and completion under the Data Archivist identity; its synthetic task transaction was rolled back. No model request, HTTP provider call or paid inference was made.

### 3.1 Effective privilege audit

- All 18 functions in sireader_workforce_private are owned by sireader_workforce_fn_owner.
- SECURITY DEFINER functions use SET search_path=pg_catalog.
- No function in the private command schema had PUBLIC EXECUTE.
- All nine public workforce base tables had RLS enabled; two policies were present per table in the staged restore.
- Seven worker permission roles and the function owner were NOLOGIN, NOINHERIT, non-superuser, non-createdb, non-createrole, non-replication and non-BYPASSRLS; no role memberships were present.
- Worker roles had zero direct SELECT/INSERT/UPDATE/DELETE grants on public workforce base tables.
- The shared lifecycle command functions were executable by the worker permission roles, but each function derives the worker identity from session_user and checks the supplied identity. Provider reservation EXECUTE was limited to Haiku, Sonnet and Opus roles.
- The read-only API role could not execute worker claim; the separate Command API role did not receive provider-reservation authority.
- Test-only maintenance and untrusted purge roles, policies and synthetic task rows were absent after transaction rollback.

The staged audit records the effective ACL state after applying the local SQL artifact; it does not assert that these same grants or policies exist in Supabase production.

## 4. Explicit production and release blockers

These tasks remain open and must not be inferred as complete from local staging:

1. **Production migration and rollback:** author a production-specific forward and reverse migration for the actual target only after schema, owner, extension, existing role and grant inventory is reviewed. The staging SQL is target-guarded and is not a production migration.
2. **Production identities:** provision dedicated per-worker LOGIN or approved pooler identities out-of-band; validate NOINHERIT, exact role membership, TLS, secret rotation and connection-pool behavior. No production worker login/password has been created.
3. **Maintenance boundary:** design and test production maintenance-only functions with explicit operation limits, batch caps, statement/time limits, safe retry behavior and audit. The existing one-shot application maintenance command retains a local-only guard; the staging purge tests do not implement a complete production maintenance API.
4. **Command API and review operator:** the eight staged functions are the ordinary worker command surface. They do not replace the separate command API or trusted human-review operator boundary. Those identities and their exact production functions still need independent staging validation.
5. **Production-equivalent staging:** the local PG17 restore does not certify the hosted Supabase Data API/PostgREST exposure, managed role topology, pooler behavior, network controls, or every production extension/default privilege. Repeat the complete migration, rollback/forward, ACL/RLS, concurrency, read-model and endpoint suite against a separately approved production-like clone.
6. **Recovery and operations:** production restore point, backup verification, restore rehearsal, rollback operator, deployment window, monitoring, alerting and incident/UNKNOWN reconciliation procedure remain to be signed off.
7. **Separate authorization:** no additional LIVE worker or paid provider request is approved. The previous one-request Haiku canary authorization is consumed. Any new finite canary must explicitly state worker/model, task/request count, synthetic input, budget caps and hard-stop window.
8. **Final acceptance:** owner operational/browser acceptance and v1.0 release sign-off remain open.

Current sanitized topology remains split: core DATABASE_URL is remote/pooler using a broad postgres identity; SIREADER_API_DATABASE_URL is remote/pooler using sireader_api_ro; Command API uses the local sireader_disposable target. WORKER_DATABASE_URL remains unset. This must be reconciled with the production migration and local-service startup topology before any activation.

## 5. Safety state and cleanup

- AI pipeline remains OFF.
- Worker runtime remains OFF/MOCK.
- Live provider remains OFF.
- WORKER_DATABASE_URL remains unset.
- Per-task and daily USD budgets remain zero/fail-closed.
- Trading remains DRY_RUN and automatic execution remains OFF.
- No production Supabase migration, role/login/credential creation, API cutover, paid inference, Telegram action, MT5/HFM connection, order or trading action occurred.
- The local PostgreSQL 17.11 cluster was stopped. Port 55439 was verified closed and the temporary review HBA file was removed. The pre-change local staging backup remains available under C:\SIREADER-STAGING.

## 6. Final disposition

**Reviewed local worker command boundary:** PASS WITH REMEDIATIONS.
**Python/PostgreSQL review-policy parity:** PASS.
**Local PostgreSQL 17.11 SQL and adapter acceptance:** PASS.
**Production migration/roles/maintenance/command/review authority:** OPEN.
**Production backup/recovery and managed-Supabase staging:** OPEN.
**Limited LIVE worker rollout:** BLOCKED / NOT AUTHORIZED.
**SIREADER Virtual Office v1.0:** NOT RELEASED.

Do not weaken the staging database guard, repurpose the staging SQL as a production migration, provision production credentials, enable LIVE flags, or infer paid-provider authorization from these results.
