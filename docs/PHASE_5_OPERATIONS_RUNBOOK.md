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

- FAST, REASONING, and ESCALATION are assigned by backend capability policy before a provider request.
- The attempt is not automatically replayed at a larger tier after an UNKNOWN provider outcome.
- Credentials are redacted before provider boundary processing.
- Personal-data indicators are redacted and cause the completed output to enter human review.
- Trading/execution capabilities and the decision_engineer/market_monitor roles are blocked from general-purpose LIVE provider routing.
- An unrecognized capability/task mapping fails closed.

## Maintenance cycle

The maintenance command performs, in one transaction:

1. Expired lease recovery.
2. Deadline task expiry.
3. Stale review expiry.
4. Expired task payload purge (pending review tasks remain protected).
5. Expired terminal review payload purge.

It does not construct a provider and does not execute tasks.

### Windows Task Scheduler

Use `scripts/run_workforce_maintenance.ps1` as the program/script. Schedule it at a cadence suitable for the deployment, commonly every 5 minutes. The script prefers the project `.venv` interpreter and expects the configured database connection in the deployment environment or local `.env`.

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
