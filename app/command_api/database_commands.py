"""Narrow PostgreSQL function adapter for workforce operator/review commands.

The legacy ORM path remains available for isolated SQLite/unit tests and older
local databases. When the complete PostgreSQL command function layer is present,
all writes go through fixed, versioned SECURITY DEFINER functions.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


COMMAND_FUNCTION_PROBE = """
SELECT num_nonnulls(
    to_regprocedure('sireader_workforce_private.command_enqueue_task_v1(text,text,text,text,jsonb,integer,integer,text)'),
    to_regprocedure('sireader_workforce_private.command_cancel_task_v1(text,text)'),
    to_regprocedure('sireader_workforce_private.command_retry_task_v1(text,text)'),
    to_regprocedure('sireader_workforce_private.command_set_worker_scheduling_v1(text,boolean,text)')
) AS present
"""

REVIEW_FUNCTION_PROBE = """
SELECT num_nonnulls(
    to_regprocedure('sireader_workforce_private.review_get_pending_v1(text)'),
    to_regprocedure('sireader_workforce_private.review_adjudicate_v1(text,text,text,jsonb,text,text,text)')
) AS present
"""


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("Workforce database command returned an invalid JSON object.")


def command_functions_available(db: Session) -> bool:
    """Return true only when the whole command API v1 function set is installed."""
    present = int(db.execute(text(COMMAND_FUNCTION_PROBE)).scalar_one())
    if present not in (0, 4):
        raise RuntimeError("Workforce command database function layer is only partially installed.")
    return present == 4


def review_functions_available(db: Session) -> bool:
    """Return true only when both trusted review functions are installed."""
    present = int(db.execute(text(REVIEW_FUNCTION_PROBE)).scalar_one())
    if present not in (0, 2):
        raise RuntimeError("Trusted workforce review database function layer is incomplete.")
    return present == 2


def enqueue_task(
    db: Session,
    *,
    task_type: str,
    title: str,
    required_capability: str,
    requested_worker_id: str,
    input_payload: dict[str, Any],
    priority: int,
    max_attempts: int,
    idempotency_key: str,
) -> dict[str, Any]:
    result = db.execute(text("""
        SELECT sireader_workforce_private.command_enqueue_task_v1(
            :task_type,:title,:required_capability,:requested_worker_id,
            CAST(:input_payload AS jsonb),:priority,:max_attempts,:idempotency_key
        )
    """), {
        "task_type": task_type,
        "title": title,
        "required_capability": required_capability,
        "requested_worker_id": requested_worker_id,
        "input_payload": json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        "priority": priority,
        "max_attempts": max_attempts,
        "idempotency_key": idempotency_key,
    }).scalar_one()
    return _json_value(result)


def cancel_task(db: Session, *, task_id: str, idempotency_key: str) -> dict[str, Any]:
    result = db.execute(text("""
        SELECT sireader_workforce_private.command_cancel_task_v1(:task_id,:idempotency_key)
    """), {"task_id": task_id, "idempotency_key": idempotency_key}).scalar_one()
    return _json_value(result)


def retry_task(db: Session, *, task_id: str, idempotency_key: str) -> dict[str, Any]:
    result = db.execute(text("""
        SELECT sireader_workforce_private.command_retry_task_v1(:task_id,:idempotency_key)
    """), {"task_id": task_id, "idempotency_key": idempotency_key}).scalar_one()
    return _json_value(result)


def set_worker_scheduling(
    db: Session, *, worker_id: str, accepting_tasks: bool, idempotency_key: str
) -> dict[str, Any]:
    result = db.execute(text("""
        SELECT sireader_workforce_private.command_set_worker_scheduling_v1(
            :worker_id,:accepting_tasks,:idempotency_key
        )
    """), {
        "worker_id": worker_id,
        "accepting_tasks": accepting_tasks,
        "idempotency_key": idempotency_key,
    }).scalar_one()
    return _json_value(result)


def get_pending_review(db: Session, *, review_id: str | None = None) -> dict[str, Any] | None:
    result = db.execute(text("""
        SELECT sireader_workforce_private.review_get_pending_v1(:review_id)
    """), {"review_id": review_id}).scalar_one()
    if result is None:
        return None
    return _json_value(result)


def adjudicate_review(
    db: Session,
    *,
    review_id: str,
    decision: str,
    expected_original_sha256: str,
    adjusted_output: dict[str, Any] | None,
    reason_code: str | None,
    notes: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    adjusted = None if adjusted_output is None else json.dumps(
        adjusted_output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    result = db.execute(text("""
        SELECT sireader_workforce_private.review_adjudicate_v1(
            :review_id,:decision,:expected_original_sha256,
            CAST(:adjusted_output AS jsonb),:reason_code,:notes,:idempotency_key
        )
    """), {
        "review_id": review_id,
        "decision": decision,
        "expected_original_sha256": expected_original_sha256,
        "adjusted_output": adjusted,
        "reason_code": reason_code,
        "notes": notes,
        "idempotency_key": idempotency_key,
    }).scalar_one()
    return _json_value(result)
