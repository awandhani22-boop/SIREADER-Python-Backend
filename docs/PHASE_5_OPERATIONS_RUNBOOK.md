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
