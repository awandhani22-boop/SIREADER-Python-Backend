# Phase 5.1 — Workforce Command API Security Design

**Status:** implementation + disposable PostgreSQL CI PASS; owner local setup pending  
**Branch:** `phase-5-implementation`  
**Parent:** Phase 4 accepted baseline  
**Deployment:** local/disposable test only until separately reviewed.

## Trust boundaries

| Component | Listener / identity | Responsibility |
|---|---|---|
| Browser | same-origin BFF | Holds only operator session cookie and in-memory CSRF token |
| Express BFF | 127.0.0.1:3000/3001 | Fixed route allowlist, operator session, CSRF, body limits, response validation |
| Observability API | 127.0.0.1:8000 / `sireader_api_ro` | GET-only snapshots; remains unchanged |
| Command API | 127.0.0.1:8010 / `sireader_workforce_cmd` | Narrow authenticated workforce mutations only |
| Worker runtime | existing opt-in process | Continues to use existing operational connection; not exposed to browser |
| Database | dedicated roles | SQL grants and RLS/trigger boundary are authoritative |

## Required command database privileges

The command API must authenticate directly as `sireader_workforce_cmd`. No role membership, no BYPASSRLS, no SUPERUSER, no CREATE, no access to non-workforce tables. Expected table actions are limited to:

- SELECT: workforce worker/capability/assignment/task/attempt rows needed for validation and idempotent replay.
- INSERT: tasks, activity events, command audit events.
- UPDATE: worker scheduling flag, task/attempt rows only for the existing safe cancel transition.
- No DELETE/TRUNCATE/ALTER/REFERENCES/TRIGGER/MAINTAIN.
- Command audit records: SELECT + INSERT only; database trigger rejects UPDATE and DELETE.

The migration requires the login role to be pre-provisioned with a private password outside Git. It must not create a known production password or modify the live database automatically.

## Command API configuration

- `SIREADER_WORKFORCE_COMMAND_DATABASE_URL` (mandatory; exact role identity checked)
- `SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN` (mandatory, distinct from `SIREADER_API_AUTH_TOKEN`)
- `WORKFORCE_COMMAND_HOST=127.0.0.1`
- `WORKFORCE_COMMAND_PORT=8010`

Missing configuration, identity mismatch, non-loopback bind, broader grants, or read-only/wrong transaction mode means startup fails closed. There is no fallback to the core writer `DATABASE_URL` and no fallback to `sireader_api_ro`.

## Audited commands

All command transactions write one append-only record keyed by `(actor_id, idempotency_key)`. The actor is assigned by the trusted BFF/command gateway and is not accepted from the browser. Store a SHA-256 fingerprint of command type + canonical request body, a correlation ID, target ID, compact outcome, and UTC timestamp. Do not store credentials or duplicate task payloads in the audit row.

- `TASK_ENQUEUE`: explicit non-trading capability/type allowlist; bounded object payload; optional eligible worker; backend repository idempotency remains in force.
- `WORKER_SCHEDULING_SET`: set `accepting_tasks`; does not change process heartbeat, worker enabled state, or current task leases.
- `TASK_CANCEL`: calls the existing `cancel_task`; if provider outcome could be unknown the existing repository transitions to `WAITING_REVIEW` and marks the attempt `UNKNOWN`.
- `TASK_RETRY`: create a *new* task only when the original is `FAILED`, last attempt is `FAILED`, no provider request ID/reservation exists, and original payload is still present. Never requeue the failed row. Never retry `WAITING_REVIEW`, `UNKNOWN`, or any provider-requested attempt.

## Worker scheduling invariant

`enabled` means the definition is enabled. `accepting_tasks` means the scheduler may claim new work. Pausing only blocks `claim_next_task`; it does not kill processes or cancel in-flight work. A paused worker can still heartbeat and finish its current leases.

## Rate limiting

The command API is a single local process bound to loopback. Enforce an actor-scoped bounded command window before opening a DB transaction. No DB side effect occurs when the limit is exhausted. The BFF also applies bounded body size/timeouts, but backend enforcement is mandatory.

## Explicit non-goals

No MT5/HFM operations, no trading signal decisions, no broker/Telegram commands, no changing runtime trading controls, no paid model invocation, no general-purpose SQL, no arbitrary proxy path, no public bind, and no production migration.
