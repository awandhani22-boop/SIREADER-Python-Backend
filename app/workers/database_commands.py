"""Authenticated database command adapter for the persistent worker runtime.

The database login identity is authoritative. Worker task mutations are performed
only by the SECURITY DEFINER command functions in sireader_workforce_private.
This module deliberately contains no ORM row mutations.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.workers.repository import GlobalProviderConcurrencyLimitError
from app.workers.runtime_contract import WorkerHeartbeatContract, WorkerContract


def _json_object(value: Any, *, operation: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Database command {operation} returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Database command {operation} returned an unexpected result.")
    return value


def _json_text(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON data.") from exc


def _call_json(db: Session, statement: str, params: dict[str, Any], *, operation: str):
    raw = db.execute(text(statement), params).scalar_one()
    if raw is None:
        return None
    return _json_object(raw, operation=operation)


def worker_get_contract(db: Session, *, worker_id: str) -> WorkerContract:
    result = _call_json(
        db,
        "SELECT sireader_workforce_private.worker_get_contract_v1(:worker_id)",
        {"worker_id": worker_id},
        operation="worker_get_contract",
    )
    if result is None:
        raise RuntimeError("Worker contract lookup returned no result.")
    return WorkerContract.model_validate(result)


def heartbeat_worker(db: Session, heartbeat: WorkerHeartbeatContract):
    result = _call_json(
        db,
        """
        SELECT sireader_workforce_private.worker_heartbeat_v1(
            :worker_id,:instance_id,:process_state,:process_started_at,
            :heartbeat_at,:active_task_count,:in_flight_provider_requests
        )
        """,
        {
            "worker_id": heartbeat.worker_id,
            "instance_id": heartbeat.instance_id,
            "process_state": heartbeat.process_state.value,
            "process_started_at": heartbeat.process_started_at,
            "heartbeat_at": heartbeat.heartbeat_at,
            "active_task_count": heartbeat.active_task_count,
            "in_flight_provider_requests": heartbeat.in_flight_provider_requests,
        },
        operation="worker_heartbeat",
    )
    return SimpleNamespace(**result)


def claim_next_task(
    db: Session,
    *,
    worker_id: str,
    instance_id: str,
    lease_seconds: int = 45,
    heartbeat_timeout_seconds: int = 60,
    now: datetime | None = None,
):
    # The SQL command uses the database clock; caller-provided time is retained
    # in this adapter signature only for compatibility with the MOCK test seam.
    result = _call_json(
        db,
        """
        SELECT sireader_workforce_private.worker_claim_next_v1(
            :worker_id,:instance_id,:lease_seconds,:heartbeat_timeout_seconds
        )
        """,
        {
            "worker_id": worker_id,
            "instance_id": instance_id,
            "lease_seconds": int(lease_seconds),
            "heartbeat_timeout_seconds": int(heartbeat_timeout_seconds),
        },
        operation="worker_claim_next",
    )
    if result is None:
        return None
    task = _json_object(result.get("task"), operation="worker_claim_next.task")
    attempt = _json_object(result.get("attempt"), operation="worker_claim_next.attempt")
    return SimpleNamespace(**task), SimpleNamespace(**attempt)


def start_task(
    db: Session,
    *,
    task_id: str,
    lease_token: str,
    worker_id: str,
    instance_id: str,
    now: datetime | None = None,
):
    result = _call_json(
        db,
        """
        SELECT sireader_workforce_private.worker_start_v1(
            :worker_id,:instance_id,:task_id,:lease_token
        )
        """,
        {
            "worker_id": worker_id,
            "instance_id": instance_id,
            "task_id": task_id,
            "lease_token": lease_token,
        },
        operation="worker_start",
    )
    return SimpleNamespace(**result)


def mark_provider_requested(
    db: Session,
    *,
    task_id: str,
    lease_token: str,
    worker_id: str,
    instance_id: str,
    provider_request_id: str | None = None,
    now: datetime | None = None,
):
    result = _call_json(
        db,
        """
        SELECT sireader_workforce_private.worker_reserve_provider_slot_v1(
            :worker_id,:instance_id,:task_id,:lease_token,:provider_request_id
        )
        """,
        {
            "worker_id": worker_id,
            "instance_id": instance_id,
            "task_id": task_id,
            "lease_token": lease_token,
            "provider_request_id": provider_request_id,
        },
        operation="worker_reserve_provider_slot",
    )
    if result.get("acquired") is not True:
        raise GlobalProviderConcurrencyLimitError(
            "The single global provider request slot is occupied or quarantined."
        )
    return SimpleNamespace(**result)


def renew_task_lease(
    db: Session,
    *,
    task_id: str,
    lease_token: str,
    worker_id: str,
    instance_id: str,
    lease_seconds: int = 45,
    now: datetime | None = None,
):
    return db.execute(
        text(
            """
            SELECT sireader_workforce_private.worker_renew_lease_v1(
                :worker_id,:instance_id,:task_id,:lease_token,:lease_seconds
            )
            """
        ),
        {
            "worker_id": worker_id,
            "instance_id": instance_id,
            "task_id": task_id,
            "lease_token": lease_token,
            "lease_seconds": int(lease_seconds),
        },
    ).scalar_one()


def complete_task(
    db: Session,
    *,
    task_id: str,
    lease_token: str,
    worker_id: str,
    instance_id: str,
    result_payload: dict,
    billing_currency: str | None = None,
    cost_amount: str | None = None,
    force_human_review: bool = False,
    review_trigger_reason: str | None = None,
    policy_context: dict | None = None,
    now: datetime | None = None,
):
    # Review policy, PII escalation, sanitization, hashes and quarantine are
    # derived/enforced in PostgreSQL. Legacy force/reason flags are never trusted.
    result = _call_json(
        db,
        """
        SELECT sireader_workforce_private.worker_complete_task_v1(
            :worker_id,:instance_id,:task_id,:lease_token,
            CAST(:result_payload AS jsonb),:billing_currency,CAST(:cost_amount AS numeric),
            CAST(:policy_context AS jsonb)
        )
        """,
        {
            "worker_id": worker_id,
            "instance_id": instance_id,
            "task_id": task_id,
            "lease_token": lease_token,
            "result_payload": _json_text(result_payload, label="result_payload"),
            "billing_currency": billing_currency,
            "cost_amount": cost_amount,
            "policy_context": _json_text(policy_context or {}, label="policy_context"),
        },
        operation="worker_complete_task",
    )
    return SimpleNamespace(**result)


def fail_task(
    db: Session,
    *,
    task_id: str,
    lease_token: str,
    worker_id: str,
    instance_id: str,
    error_code: str,
    retryable: bool = False,
    outcome_unknown: bool = False,
    backoff_seconds: int = 30,
    now: datetime | None = None,
):
    result = _call_json(
        db,
        """
        SELECT sireader_workforce_private.worker_fail_task_v1(
            :worker_id,:instance_id,:task_id,:lease_token,:error_code,
            :retryable,:outcome_unknown,:backoff_seconds
        )
        """,
        {
            "worker_id": worker_id,
            "instance_id": instance_id,
            "task_id": task_id,
            "lease_token": lease_token,
            "error_code": error_code,
            "retryable": bool(retryable),
            "outcome_unknown": bool(outcome_unknown),
            "backoff_seconds": int(backoff_seconds),
        },
        operation="worker_fail_task",
    )
    return SimpleNamespace(**result)
