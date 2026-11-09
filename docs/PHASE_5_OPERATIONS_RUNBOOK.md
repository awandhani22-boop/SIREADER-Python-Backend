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

Use `scripts/run_workforce_maintenance.ps1` as the program/script. Register it in Windows Task Scheduler at a cadence suitable for the deployment, commonly every 5 minutes. The script prefers the project `.venv` interpreter and expects the configured database connection in the deployment environment or local `.env`.

**Important:** the repository contains a one-shot command and wrapper only. It does not create or register a Windows scheduled task automatically. Verify the operator account, working directory, database environment, task history, and log output after the deployment owner registers it.

Check the task's last result and application logs. The output contains aggregate counts only, not task payloads or secrets.

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
- A retention/inspection failure is a hard error; do not bypass the guard or manually clear database columns.

## Incident response

- Provider outcome UNKNOWN: stop replay/escalation; reconcile the Cost Guard reservation first.
- Stale review/hash conflict: reload the detail and compare the current hash.
- Budget hard limit: stop new provider reservations; do not modify the budget in order to bypass the guard.
- Policy rejection: inspect the safe reason code and capability mapping; do not downgrade the worker policy.
- Retention guard failure: stop purge and investigate schema/view/privilege health.


## Verified implementation snapshot — 2026-11-10

- Backend branch: `phase-5-implementation`.
- Backend head at this snapshot: `1568435e092fc3b59bb271a9eef9d24981daa8e8`.
- [CI run 42671067597 PASS](https://github.com/awandhani22-boop/SIREADER-Python-Backend/actions/runs/42671067597): 187 API tests passed, 76 core regression tests passed, compile step successful.
- [Budget observability CI run 42667794174 PASS](https://github.com/awandhani22-boop/SIREADER-Python-Backend/actions/runs/42667794174): 177 API tests and 75 core regression tests passed.
- These checks use disposable test infrastructure. They do not deploy migrations to Supabase LIVE, start a paid provider, or register an OS-level maintenance task.
