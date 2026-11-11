# SIREADER Workforce — Phase 5 Operations Runbook

## Scope and safety

This runbook covers generic workforce operations only. It does not authorize the Telegram trading pipeline, MT5/HFM, broker execution, paid provider usage, Supabase LIVE changes, or enabling worker auto-execution.

The default runtime remains fail-closed:

- `WORKER_RUNTIME_ENABLED=false`
- `WORKER_PROVIDER_MODE=MOCK`
- `WORKER_LIVE_PROVIDER_ENABLED=false`
- task-level provider cost caps remain zero until separately approved

## Human Review

1. Open the Review Inbox only from an authenticated LIVE operator session.
2. Verify the review ID, task, attempt, original output hash, expiry, and output content.
3. Choose APPROVE, EDIT, or REJECT.
4. For EDIT, validate the entire JSON object before submitting.
5. On a conflict or stale hash, refresh the review; do not force a decision.
6. Confirm that the review reached a terminal state and the command audit entry exists.
7. Never copy quarantined output into an ordinary task response before successful adjudication.

## Budget governance

The Cost Guard remains the sole usage/reservation ledger. Workforce cost observability is a read-only projection over usage records and task attempts.

Configured targets:

| Budget | Amount |
|---|---:|
| Daily IDR target | Rp10,000 |
| Daily IDR hard limit | Rp50,000 |
| Rolling 30-day IDR | Rp50,000 |
| FAST allocation | Rp25,000 |
| REASONING allocation | Rp22,500 |
| ESCALATION allocation | Rp2,500 |
| Anthropic monthly budget | USD 5 |

Reserved and UNKNOWN requests must continue to count toward projected usage until reconciled. Never interpret UNKNOWN as zero cost. IDR and USD are displayed separately; do not sum or convert them without an approved exchange-rate source.

## Model and sensitive-task policy

Policy version: `2026.11.10`.

- Policy version `2026.11.10` validates worker, required capability, and task-type compatibility before the provider boundary.
- FAST capabilities: `quick_research`, `information_extraction`, `summarization`, `lightweight_classification`.
- REASONING capabilities: `data_analysis`, `comparison`, `evaluation`, `structured_reasoning`.
- ESCALATION capabilities: `complex_reasoning`, `strategic_analysis`, `deep_review`. `decision_evaluation` is explicitly barred from general-purpose LIVE provider routing because it is classified as trading-adjacent.
- Worker tier ceilings are fixed: Haiku=FAST, Sonnet=REASONING, Opus=ESCALATION. The provider independently verifies that the request's explicit tier equals the worker ceiling; it never silently falls back to another tier.
- Capability/task selection is the bounded escalation decision and happens before the single provider call. The current runtime does not perform quality-based multi-call escalation within one attempt.
- The attempt is not automatically replayed at a larger tier after an UNKNOWN provider outcome. Reconcile the existing Cost Guard reservation first.
- Credentials are redacted before provider boundary processing.
- Pattern/key-based personal-data indicators (email, phone/mobile, passport, government/personal ID, date of birth, address, postal/ZIP code, SSN, tax ID, and name fields) are redacted and cause the completed output to enter human review. This is a deterministic guardrail, not a guarantee of complete PII detection or a substitute for an approved data-loss-prevention service.
- Trading/execution capabilities and the decision_engineer/market_monitor roles are blocked from general-purpose LIVE provider routing.
- An unrecognized capability/task mapping fails closed.
- Private-key markers, explicit live trade/order instructions, execution-related payload keys, trading capability sets, and Decision Engineer / Market Monitor LIVE provider requests are rejected.
- Mock-mode simulation can continue for excluded worker identities, but it never receives a provider model tier.

## Maintenance cycle

The maintenance command performs, in one transaction:

1. Expired lease recovery.
2. Deadline task expiry.
3. Stale review expiry.
4. Expired task payload purge (pending review tasks remain protected).
5. Expired terminal review payload purge.

It does not construct a provider and does not execute tasks.

### Windows Task Scheduler

The repository includes a one-shot runner and a registration script:

- `scripts/run_workforce_maintenance.ps1` runs one maintenance transaction.
- `scripts/register_workforce_maintenance.ps1` registers/removes the Windows scheduled task.

From an elevated or ordinary PowerShell session under the intended Windows user, run:

```powershell
.\scripts\register_workforce_maintenance.ps1
```

It registers `SIREADER-WorkforceMaintenance` every 5 minutes using the current interactive account (`/IT`); no Windows password is stored. Add `-RunNow` to start one cycle after registration. Remove it with:

```powershell
.\scripts\register_workforce_maintenance.ps1 -Remove
```

Both the Python maintenance command and the Windows registration script fail closed unless every configured `DATABASE_URL` source targets PostgreSQL on a loopback host (`localhost`, `127.0.0.1`, or `::1`). The registration script checks the project `.env` plus process/user/machine `DATABASE_URL` overrides without printing credentials. The Python command independently rejects libpq host/service override variables (`PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGSERVICE`, `PGSERVICEFILE`, `PGLOADBALANCEHOSTS`, `PGTARGETSESSIONATTRS`, and `PGSSLMODE`) and URL query parameters before connecting. It creates a dedicated one-connection maintenance engine and disposes it after the run; it never imports the application's shared database engine or changes its Supabase SSL settings. Remote, Supabase, and production hosts are blocked.

The maintenance cycle expires stale reviews and can purge expired task/review payloads, so it is a data-mutating operation. The current `app/database/connection.py` documents a Supabase Shared Pooler connection; do not assume that the existing `.env` is local or safe. A loopback-only configuration must be positively established before one-shot execution or scheduling. No Supabase LIVE maintenance is authorized by this runbook.

After the database target has been independently verified as non-LIVE and the owner authorizes local scheduling, verify the task's last result and application logs after registration. Registration does not enable the workforce worker runtime or any provider.

**Important:** these scripts are committed but cannot register a task on the owner's Windows machine from GitHub CI. The deployment owner must run the registration command locally.

### One-shot manual execution

From the backend project root:

```powershell
.\.venv\Scripts\python.exe -m app.workers.maintenance
```

Do not run concurrently with a manually started database migration. The routine is transactional and idempotent for normal repeated scheduling.

## Retention and audit

- Pending reviews are not purged.
- Review payloads are eligible only after a terminal review and parent task retention expiry.
- Purged payloads cannot be restored.
- Permanent review metadata and audit events remain available after payload purge.
- Initial review metadata must be inserted as PENDING with no adjudication fields. Initial payload INSERT must contain sanitized original output only, while the review remains PENDING.
- Incremental review migration v3 removes INSERT on review metadata and payload tables from `sireader_workforce_cmd`; the worker completion boundary is the only review-creation path. The command role only adjudicates existing reviews using its restricted UPDATE columns.
- A retention/inspection failure is a hard error; do not bypass the guard or manually clear database columns.

## Incident response

- Provider outcome UNKNOWN: stop replay/escalation; reconcile the Cost Guard reservation first.
- Stale review/hash conflict: reload the detail and compare the current hash.
- Budget hard limit: stop new provider reservations; do not modify the budget in order to bypass the guard.
- Policy rejection: inspect the safe reason code and capability mapping; do not downgrade the worker policy.
- Retention guard failure: stop purge and investigate schema/view/privilege health.


## Verified implementation snapshot — 2026-11-10

- Backend branch: `phase-5-implementation`.
- Latest backend code commit: `1c8277ea1ccf0ff0b81966b0d8bd6516829e1494`.
- [Latest backend CI run 42700770005 PASS](https://github.com/awandhani22-boop/SIREADER-Python-Backend/actions/runs/42700770005): 196 API tests passed, 76 core regression tests passed, and API compile succeeded.
- Review creation boundary v3 [CI run 42700267022 PASS](https://github.com/awandhani22-boop/SIREADER-Python-Backend/actions/runs/42700267022): 190 API tests and 76 core tests passed. It enforces PENDING-only metadata INSERT and removes command-role INSERT privileges.
- Maintenance database safety guard [CI run 42700636167 PASS](https://github.com/awandhani22-boop/SIREADER-Python-Backend/actions/runs/42700636167): 196 API tests and 76 core tests passed.
- `scripts/register_workforce_maintenance.ps1` registers/removes the five-minute task but has not been executed on the owner's machine; no OS-level task is claimed as registered.
- CI uses disposable test infrastructure. It does not deploy migrations to Supabase LIVE, start a paid provider, inspect the owner's `.env`, or register a local Windows scheduled task.
- CI uses disposable test infrastructure. It does not deploy migrations to Supabase LIVE, start a paid provider, or register a local Windows scheduled task.


## Verified local runtime recovery and API startup — 2026-11-10

The laptop already has PostgreSQL 16.6 installed and running. **Do not reinstall PostgreSQL or create another local application database.**

- PostgreSQL: `127.0.0.1:5432`; data directory `C:\Users\ASUS\.gemini\antigravity\scratch\pgsql\data`.
- Existing disposable application database: `sireader_disposable`. Both `SIREADER_API_DATABASE_URL` and `SIREADER_WORKFORCE_COMMAND_DATABASE_URL` in this backend `.env` point to it.
- The populated `sireader_test` database is not a scratch target; do not drop tables, reset it, or use it for fixture setup.
- Reuse Python 3.12 at `C:\SIREADER\.venv\Scripts\python.exe`; no new virtual environment is needed.
- Local review security migration `2026_11_10_workforce_review_command_security_v1.sql` has already been applied to `sireader_disposable`. Do not apply it to Supabase LIVE.

Start the existing APIs in separate PowerShell windows from the backend root:

```powershell
Set-Location 'C:\Users\ASUS\.gemini\antigravity\scratch\SIREADER-Python-Backend'
& 'C:\SIREADER\.venv\Scripts\python.exe' -m app.api.server
```

```powershell
Set-Location 'C:\Users\ASUS\.gemini\antigravity\scratch\SIREADER-Python-Backend'
& 'C:\SIREADER\.venv\Scripts\python.exe' -m app.command_api.process
```

Start the existing frontend/BFF launcher from its root:

```powershell
Set-Location 'C:\Users\ASUS\.gemini\antigravity\scratch\SIREADER'
npm run dev
```

Expected loopback ports: Python API `8000`, command API `8010`, Vite `3000`, BFF `3001`. **Do not run `python -m app.main` for these APIs**; it is the legacy Telegram application entrypoint.

The Phase 5 maintenance command is intentionally not activated. Do not configure maintenance `DATABASE_URL`, execute purge, or register the Task Scheduler job until owner browser acceptance is complete and the owner separately authorizes that data-mutating operation. No Telegram worker, AI provider, or trading execution should be started for API acceptance.

Latest local verification: frontend 415 tests + TypeScript + production build pass; backend 172 pass / 42 skipped. The skipped PostgreSQL integration tests require an isolated `SIREADER_TEST_POSTGRES_URL`; never point them at a populated application database. BFF/browser LIVE DTO contracts now include the Phase 5.2.1 task lineage and activity audit fields.


### Retry activity RLS follow-up — 2026-11-10

The additive migration `2026_11_10_workforce_review_command_security_v2.sql` is also applied to local `sireader_disposable`. It preserves the Phase 5.1 retry command by allowing a `TASK_RETRIED` event only when both the original and newly queued task are visible to the non-trading command role. Direct insert as `sireader_workforce_cmd` passed inside a rolled-back transaction; no test event remains. The migration is included in the disposable PostgreSQL CI fixture. Do not apply either review security migration to Supabase LIVE.


### Review adjudication and append-only audit follow-up — 2026-11-10

Local migration `2026_11_10_workforce_review_command_security_v3.sql` is applied to `sireader_disposable`. It permits the command role to lock a PENDING review payload with `SELECT ... FOR UPDATE`, but only permits the row update to pass RLS when the parent review is EDITED. The existing write-once payload trigger remains active.

The activity ORM mapping now disables implicit `RETURNING` for `WorkActivityEventRecord`, so inserting an audit event does not require SELECT privilege on `event_seq` or the append-only activity ledger. The command role remains INSERT-only on that ledger.

A transaction-scoped local verification passed APPROVE (original payload release), EDIT (sanitized adjusted payload release), and both immutable review audit inserts under `sireader_workforce_cmd`. All synthetic rows/events were rolled back; no acceptance fixture remains. Backend local suite: 172 passed / 44 skipped; skipped PostgreSQL integration coverage runs in CI with `SIREADER_TEST_POSTGRES_URL`.


### REJECT adjudication verification — 2026-11-10

A transaction-scoped probe under `sireader_workforce_cmd` passed REJECT: the review transitions to REJECTED, task `result_payload` remains NULL (the quarantined output is not released), and `HUMAN_REVIEW_REJECTED` is appended. Synthetic task/review/payload/event rows were rolled back. Database-level APPROVE, EDIT and REJECT paths now all pass; authenticated browser acceptance remains a separate open gate.


## Local disposable maintenance identity and scheduler — 2026-11-10

- The maintenance job uses a separate SIREADER_WORKFORCE_MAINTENANCE_DATABASE_URL; it deliberately does not populate the application's general DATABASE_URL.
- Local .env target: postgresql+psycopg://sireader_workforce_maint@127.0.0.1:5432/sireader_disposable. The local PostgreSQL 127.0.0.1 host rule currently uses trust; the dedicated login has no password and is not a superuser, cannot bypass RLS, cannot inherit roles, and is granted only the workforce maintenance tables/columns and activity INSERT/sequence USAGE.
- Migration: migrations/2026_11_10_workforce_maintenance_role_v1.sql; rollback: migrations/2026_11_10_workforce_maintenance_role_v1_rollback.sql. Both refuse any database other than loopback sireader_disposable. RLS confines task, attempt, review and payload access to the explicit non-trading worker/capability/type allowlist. The role cannot read worker registry, heartbeats, command audit, or trading/Telegram tables; it has no DELETE privilege.
- The Python maintenance entrypoint independently verifies the connected database, host, exact role name, and non-privileged role flags before opening a maintenance session. The runner selects the project .venv or the verified Python 3.12 environment at C:\SIREADER\.venv.
- The Windows registration script validates the dedicated URL from .env and every Process/User/Machine override, rejects query/fragment and all libpq redirection overrides, and requires exactly sireader_workforce_maint on sireader_disposable. It registers SIREADER-WorkforceMaintenance every 5 minutes for the current interactive Windows user (/IT); the user must be logged in for the task to run.
- First authorized local maintenance run: 4 stale PENDING reviews transitioned to EXPIRED and 4 expired task payloads were purged. No task lease recovery or deadline expiry occurred; no review payload was purged. A second run returned zero for every operation, confirming idempotency. Review metadata and audit events remain; pending reviews are not purged.
- Never copy this local URL or role to Supabase LIVE or another database. If the local PostgreSQL authentication rules change from loopback trust, the scheduled task should fail closed until an explicit authenticated local credential is provisioned.

## Final Phase 5 verification snapshot - 2026-11-10 16:42 local

This section supersedes earlier operational-status notes in this runbook where they conflict.

- Frontend full local suite: `npm test` **416 passed, 0 failed**; `npm run lint` passed; `npm run build` passed with the existing non-blocking large-chunk warning.
- Backend supported suite with PostgreSQL integration enabled against a separate loopback PostgreSQL 16.6 cluster: **229 passed, 0 failed, 0 skipped**, one non-blocking Starlette/httpx deprecation warning. Event migration test passed 1/1 after its prerequisite fixture. Backend `compileall -q app` passed.
- The test cluster on port 55432 was gracefully stopped after the run. The separate scratch database `sireader_phase5_test_20261110` mistakenly created on the application PostgreSQL cluster was inspected, confirmed to contain only fixture-created test objects/data with zero active sessions and no environment references, then dropped. The configured `sireader_disposable` application database was not dropped, reset, or redirected.
- Services are listening on loopback ports 3000, 3001, 8000, and 8010. Browser operator session is authenticated; the Virtual Office simulation remains in DEMO while its backend connection is LIVE.
- Review Inbox empty-state read passed. The read-only application role `sireader_api_ro` confirmed zero review rows and zero PENDING in `sireader_disposable`. Frontend DTO tests cover pending list/detail/adjudication command contracts, BFF tests cover review list/detail proxy boundaries, and backend PostgreSQL tests cover APPROVE/EDIT/REJECT, release/quarantine, RLS and audit. No manual UI decision was made because there was no legitimate PENDING record; no fixture was inserted into the application DB.
- `SIREADER-WorkforceMaintenance` runs every five minutes under the interactive current user at Limited privilege. Battery restrictions are disabled and `StartWhenAvailable=True`. Automatic Task Scheduler runs at 16:36:01 and 16:41:01 both completed with `LastTaskResult=0`; next run 16:46. Maintenance does not start workers or providers.
- Backend local commit: `10a2f96`; frontend implementation/docs commits remain local. No GitHub push/merge.
- Phase 5 implementation and applicable automated/operational acceptance are complete. The next real PENDING review is an owner-operated business action, not a fabricated acceptance fixture. Phase 6 remains unstarted.
- No Supabase LIVE migration, paid provider call, Telegram, MT5/HFM, or trading action was performed.

### Fresh PostgreSQL cluster replay - 2026-11-10 16:44 local

- Reinitialized a fresh PostgreSQL 16.6 test cluster and empty `sireader_test` database on isolated loopback port 55432. The privilege/security fixture preflight passed 20/20; the subsequent complete backend `tests/` suite passed **229 passed, 0 failed, 0 skipped** (one Starlette/httpx deprecation warning).
- The test cluster was gracefully stopped and both temporary test data directories were removed. Port 55432 is free. No application database or Supabase LIVE target was modified.


## Phase 7.1 — Offline worker runtime assurance — 2026-11-10

Added a dedicated worker unit-test suite:
- `tests/workers/test_provider_unit.py` verifies deterministic MOCK output, cancellation, provider factory switches, credential and model-tier guards, zero per-task cap rejection, fake OmniRoute success parsing, and UNKNOWN handling for missing usage.
- `tests/workers/test_policy_unit.py` verifies capability/task matching, personal-data review, private-key/trading-instruction rejection, allowed provider roles, and tier ceilings.
- `tests/workers/test_runtime_guards.py` verifies that a disabled worker runtime fails before any heartbeat/database side effect and rejects invalid polling intervals before process startup.
- `tests/workers/test_runtime_mock_flow.py` exercises the actual `run_worker_once` orchestration with fake DB sessions and the real deterministic `MockWorkerProvider`; it verifies a SIMULATED completion, empty queue behavior and rejection of a trade instruction before provider creation.
- `tests/workers/conftest.py` supplies process-local synthetic settings. The worker unit tests do not load real `.env` secrets and do not establish a DB connection.

Verification (2026-11-10): 31 new focused tests passed; full local backend suite **216 passed, 44 skipped, 0 failed**. Existing PostgreSQL integration tests were skipped because `SIREADER_TEST_POSTGRES_URL` was not set. One existing Starlette/httpx deprecation warning remains.

No OS worker process was started and no real provider/network call, application database write, Supabase LIVE operation, Telegram action or trading action occurred during Phase 7.1.

Next: Phase 7.2 is one foreground Haiku worker in MOCK mode against a newly created temporary PostgreSQL 16.6 cluster and a synthetic non-sensitive task. Never use `sireader_disposable` or another populated database as a test fixture. Phase 7.3 LIVE provider canary requires a separate explicit provider/model and budget approval.


## Phase 7 — Central AI Harness implementation update — 2026-11-10

See `docs/PHASE_7_AI_HARNESS.md` for the architecture, worker profiles, direct Anthropic request lifecycle, model price table, usage reporting and activation gates.

Implementation update:
- One central Anthropic Messages API gateway is shared by the trading AI pipeline and the AI-capable workforce provider. The historical `omniroute_client.py` path is an import-compatibility shim; the application no longer sends `/chat/completions` requests.
- Direct model IDs are configured as `claude-haiku-5-5`, `claude-sonnet-5-5`, and `claude-opus-5-5`; agreed output ceilings are 700 / 1,400 / 1,800 tokens.
- Before any billable Messages request, the gateway calls Anthropic's `POST /v1/messages/count_tokens`, uses its model-specific input-token result for the USD reservation, then dispatches one Messages request. If count preflight fails, the paid request is not sent. A post-dispatch uncertain outcome remains UNKNOWN and is not retried.
- Production endpoint validation accepts only `https://api.anthropic.com`; custom HTTPS hosts are rejected so the API key is never sent to a proxy by accident.
- Seven worker profiles and one central route resolver are present. Supervisor, Decision Engineer, Market Monitor and Data Archivist use bounded deterministic Python handlers; Haiku, Sonnet and Opus are the only profiles allowed to use the shared AI gateway. Supervisor remains deterministic by default.
- Python prefilter skips only explicitly pure COMMENTARY bundles. Missing roles, unknown context, replies, edits, episode links and trade-related roles fail open to model analysis. Messages remain in stored history.
- Anthropic USD usage can be reported by model, analysis, worker and task. Worker/task association uses the persisted request ID shared by `AIUsageLog.request_id` and `WorkTaskAttemptRecord.provider_request_id`; no schema migration was needed.
- Truncated AI output returns MANUAL_REVIEW. Paid provider requests are not retried automatically after an unknown outcome.

Offline focused verification in the project Python 3.12 environment (`C:\SIREADER\.venv`): **195 passed, 17 skipped, 0 failed**. The skipped tests require the isolated PostgreSQL integration fixture. `python -m compileall -q app tests/workers`, `python -m pip check`, and `git diff --check` all passed in this environment.

The broader `pytest -q --ignore=test_connection.py` run using an intentionally unused loopback database URL did not finish and was stopped after remaining stalled without test output. `test_connection.py` itself exits at collection when the owner's actual `.env` database URL is absent. The full integration suite must be run with the project's disposable PostgreSQL fixture; never point it at `sireader_disposable` or Supabase LIVE.

The unrelated global Python installation has FastAPI 0.162.2 / Starlette 0.37.2 dependency inconsistency. It was not used for final project verification and no package upgrade was made.

Operational switches remain unchanged and fail-closed: `AI_PIPELINE_ENABLED=False`, `WORKER_RUNTIME_ENABLED=False`, `WORKER_PROVIDER_MODE=MOCK`, `WORKER_LIVE_PROVIDER_ENABLED=False`, `WORKER_MAX_TASK_COST_USD=0`. No API key was found in the selected backend environment. No live Anthropic request, worker start, database migration/write, Supabase LIVE operation, Telegram action, trading action, Git commit or push occurred.


### Phase 7 final verification addendum — 2026-11-10

Supersedes the earlier Phase 7 test counts in this runbook and `docs/PHASE_7_AI_HARNESS.md`:
- Isolated API/PostgreSQL integration group: **229 passed, 0 failed, 0 skipped**.
- Central AI Harness/provider/billing/worker unit group: **212 passed, 0 failed**.
- Root backend regression tests (40 actual pytest files; manual smoke scripts excluded): **370 passed, 0 failed**.
- Compileall passed for `app`, `tests/workers`, `tests/api` and `app/command_api`; project-venv `pip check` passed; `git diff --check` passed.
- PostgreSQL tests used a fresh temporary PostgreSQL 16.6 instance on loopback port 55432, database `sireader_test`, entirely separate from the application's existing local database and Supabase. The temporary instance is removed after validation.
- These groups overlap and are reported separately; their test counts must not be summed as unique tests.
- A non-blocking Starlette/httpx TestClient deprecation warning remains.
- Root `test_*.py` includes manual smoke scripts that make top-level database/MT5 calls; do not blindly collect them with pytest. The final root run selected only files with actual pytest test functions/classes.
- No live Anthropic call, worker process, Telegram action, MT5 action, Supabase LIVE change, owner DB mutation, Git commit or push was performed. All live switches remain fail-closed. A live-model canary and production activation are separate operator approvals, not part of offline implementation acceptance.



## Phase 7.2 — MOCK worker runtime acceptance (2026-11-10)

- Executed the actual one-cycle Haiku runtime entrypoint `run_worker_once` against a fresh isolated PostgreSQL 16.6 cluster on loopback port 55432, database `sireader_test`; all seven canonical worker profiles/capabilities were seeded by the workforce migrations.
- Deterministic QUICK_RESEARCH sample case: task and attempt COMPLETED, source SIMULATED, review PENDING, output remained quarantined (`task.result_payload=NULL`), no provider request/billing/cost, heartbeat STOPPED.
- Deterministic non-sample case: task and attempt COMPLETED, source SIMULATED, no review row, normal result payload release, no provider request/billing/cost, heartbeat STOPPED.
- Found and fixed a mismatch between the policy's no-review branch and the database result-release trigger. Added `migrations/2026_11_10_workforce_review_release_boundary_v2.sql` plus rollback. It allows trusted worker output only when no review row exists; PENDING reviews remain blocked, and `sireader_workforce_cmd` still cannot bypass approved/edited review adjudication.
- Database guard probes passed: unapproved command-role release BLOCKED; PENDING review release BLOCKED. The additive migration was applied only on the isolated disposable test database, never on the application database or Supabase LIVE.
- The temporary SQLAlchemy engine used `sslmode=disable` only for this local PostgreSQL test server, which did not provide SSL; production/application connection configuration was not changed.
- No long-running worker process, Anthropic call, Telegram/MT5 action, production DB write, Supabase LIVE operation, Git commit or push occurred. Temporary task rows are synthetic and are deleted with the temporary PostgreSQL cluster.


### Phase 7.2 foreground-process acceptance addendum — 2026-11-11

The full process-level gate subsequently passed on a fresh SSL-enabled PostgreSQL 16.6 temporary cluster, loopback port 55441, database `sireader_test`. A test-only, non-superuser runtime login and RLS policy restricted the OS worker to Haiku. A temporary authenticated read-only API ran on loopback port 18000.

A foreground `python -m app.workers.process --worker haiku --poll-interval 0.5` process was visible as READY through the API, claimed one benign synthetic QUICK_RESEARCH task, and completed it as SIMULATED. The API timeline showed TASK_CLAIMED → TASK_STARTED → HUMAN_REVIEW_QUEUED → TASK_COMPLETED. The PENDING review appeared in the review API after a missing `sireader_view_owner` RLS policy was corrected in the legacy review migration; result_payload remained NULL and the original output remained quarantined. Attempt provider ID/currency/cost were NULL.

CTRL+C shutdown set the heartbeat to STOPPED and exited the first process. A second process instance started with a new instance ID, returned READY, and shut down cleanly to STOPPED. A test-only heartbeat timestamp change (two minutes old) caused the read-only API to report STALE. The temporary API, cluster, credentials, certificate/key and test directory were stopped/removed. The main app DB and Supabase LIVE were not touched.

The migration fix and regression test now make the API review view show a PENDING record only through the security-barrier view while direct base-table reads remain denied. The focused PostgreSQL privilege/review-visibility suite passed **22/22** after the fix. Full offline backend `tests/` after changes: **237 passed, 46 skipped, 0 failed**; compileall and pip check passed. Skips require a separate disposable PostgreSQL URL.
