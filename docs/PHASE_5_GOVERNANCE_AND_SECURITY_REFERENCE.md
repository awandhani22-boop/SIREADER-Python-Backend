# Phase 5 — Workforce Governance & Security Reference

**Policy version:** `2026.11.10`  
**Branch:** `phase-5-implementation`  
**Scope:** Generic SIREADER workforce tasks only.

## Provider boundary

A task must pass the following backend-owned checks before any provider request:

1. Required capability exists in the canonical capability catalog.
2. The selected worker owns that capability.
3. The task type is explicitly supported by the capability contract.
4. The provider mode is exactly MOCK or the explicitly configured provider mode.
5. For LIVE provider mode, the worker must be one of Haiku, Sonnet, or Opus.
6. The selected tier must match both the capability mapping and the registered worker tier ceiling.

The model mapping is:

| Model tier | Capability family | Worker ceiling |
|---|---|---|
| FAST | Quick research, extraction, summarization, lightweight classification | Haiku |
| REASONING | Analysis, comparison, evaluation, structured reasoning | Sonnet |
| ESCALATION | Complex reasoning, strategic analysis, deep review | Opus |

The backend provider checks the explicit tier again. Missing, unknown, or mismatched tier fails closed. No fallback to `worker.model_tier` is performed on invalid input.

This design selects the appropriate tier before a provider request. It deliberately does not issue an additional provider call when output quality is uncertain or provider outcome is UNKNOWN. A new, separately authorized task would be required for any later human-directed rework; unresolved provider reservations must be reconciled first.

## Sensitive-task decisions

| Decision | Provider behavior | Output behavior |
|---|---|---|
| ALLOW | Use the selected tier and validated input | Existing capability review policy still applies |
| REDACT | Scrub credential-like keys and patterns before provider dispatch | No forced review solely because redaction occurred |
| REVIEW | Redact detected personal identifiers before dispatch | Force atomic output quarantine and human review |
| REJECT | Do not dispatch a provider request | Fail the task with a safe policy reason code |

The current detector uses deterministic key markers and patterns. It is not a general-purpose DLP engine and cannot guarantee detection of names or personal data embedded in arbitrary free text. Inputs that need stronger assurance must be constrained by schema and minimized before enqueue.

## Hard exclusions

General-purpose LIVE provider routing rejects:

- Trading-related capabilities in the governance trading-capability set.
- `decision_engineer` and `market_monitor` workers.
- Explicit live trade/order execution language.
- Execution/broker/MT5/HFM command keys.
- Private-key markers.
- Unknown worker, capability, task-type, or model-tier combinations.

These checks do not modify or replace the separate Telegram trading pipeline, decision engine, approval flow, MT5, or HFM. Those systems remain outside the Workforce provider boundary.

## Cost and retry invariant

The existing Cost Guard is the only usage/reservation ledger. One workforce attempt retains one provider request ID. Provider outcome UNKNOWN is not a retryable failure and must not be escalated to another provider invocation until reconciled. Reserved/UNKNOWN usage remains included in projected budget consumption.

## Human review and retention

- Output requiring review is sanitized and quarantined in the same completion transaction.
- The ordinary task `result_payload` remains NULL while review is pending.
- Adjudication requires a matching original-output SHA-256 and an idempotency key.
- Review metadata remains after payload purge.
- Pending reviews and parent task payloads remain protected until their respective retention/terminal conditions are satisfied.
- The view owner receives only required column-level grants; the browser API role is denied direct table access.

## Operational readiness

The worker runtime defaults remain disabled/Mock. No paid provider call is required for CI. The maintenance command is a one-shot transactional command and must be registered with the deployment scheduler by the operator. This repository does not prove that a Windows scheduled task has been registered.

## CI evidence

[Backend final policy/maintenance CI run 42671067597](https://github.com/awandhani22-boop/SIREADER-Python-Backend/actions/runs/42671067597) — 187 API tests and 76 core tests passed.
