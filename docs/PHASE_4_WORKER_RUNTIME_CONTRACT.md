# Phase 4.1 — Worker Runtime Contract

**Contract version:** 1. **Implementation:** app/workers/runtime_contract.py and app/workers/registry.py.

## Authority and scope

The Python backend is authoritative for LIVE workforce state. The Virtual Office's existing scheduler remains a deterministic SIMULATED system. This contract deliberately does not import frontend code, SQLAlchemy models, provider SDKs, Telegram, or MT5.

Worker identity matches the existing frontend stable IDs: supervisor, haiku, sonnet, opus, decision_engineer, market_monitor, and data_archivist. Capability IDs and per-worker concurrency are aligned with the existing Phase 2.1 frontend capability registry.

## Independent facts (do not infer one from another)

- Worker definition: configured identity, role, department, capabilities, enabled flag, concurrency limit, and approved model tier.
- Process heartbeat: an instance's lifecycle state and heartbeat timestamp.
- Task lifecycle: queue, claim, run, retry, review, completion, failure, cancellation, or dead-letter status.
- Task attempt: a single claim/execution attempt, including provider-request state and cost record reference.
- Provider request: a request routed through the existing cost guard. It is not inferred from a process heartbeat or avatar state.
- Activity event: immutable, correlated lifecycle evidence.

A worker may be configured while no process is running. A READY heartbeat does not imply a task is executing. A RUNNING task does not imply a paid model request is active. A task cannot be presented as completed until its authoritative backend transition is persisted.

## Contract versioning

All public contract models have schema_version=1 and reject unknown fields. Payloads are bounded (64 KiB for task input/result and 16 KiB for event payload). IDs and enum values are validated. Breaking changes require a new version and an explicit API compatibility/deprecation window; additive optional fields may be introduced only when old readers safely ignore them.

## Stable roster

| Worker ID | Role | Concurrency | Model tier | Capability family |
|---|---|---:|---|---|
| supervisor | Central Router & Task Orchestrator | 2 | NONE | intake, decomposition, routing, oversight, review |
| haiku | Fast Research & Intake Specialist | 2 | FAST | research, extraction, summary, classification |
| sonnet | Deep Quantitative & Structural Analyst | 1 | REASONING | analysis, comparison, evaluation, structured reasoning |
| opus | High-Complexity Escalation Architect | 1 | ESCALATION | complex reasoning, strategy, deep review |
| decision_engineer | Risk Guardrail & Rule Validator | 1 | NONE | rule validation and hypothetical risk checks |
| market_monitor | Market Feed & Volatility Observer | 1 | NONE | supplied/simulated market observation only |
| data_archivist | Audit Ledger & Historical State Curator | 1 | NONE | archive, index, history, organization |

Registry definitions do not claim process liveness. Runtime heartbeats are independent records.

## Task status transitions

Allowed transitions are defined in TASK_TRANSITIONS. QUEUED may be claimed or cancelled/dead-lettered. CLAIMED can start or return to queue before provider execution. RUNNING may complete, fail, enter bounded retry, wait for review, cancel, or dead-letter. RETRY_WAIT may requeue. WAITING_REVIEW is a deliberate stop for ambiguous/unknown outcomes and can only be explicitly resolved. Terminal states cannot be reopened.

Unknown provider outcome is never automatically retried. This prevents duplicate paid execution after timeout/restart.

## Capability boundary

Capability strings match the existing frontend catalog. Market monitoring and risk assessment names explicitly identify simulation/observation; these capabilities have no order execution authority. Phase 4 never places, modifies, or closes MT5 positions and does not bypass the Python Decision Engine or manual approval.

## Verification

test_worker_runtime_contract.py verifies canonical IDs/capabilities, registry eligibility, version/extra-field rejection, JSON payload limits, lifecycle transition rules, independent heartbeat/task/request counters, and typed attempts/events. CI uses no database, secret, or paid provider request.