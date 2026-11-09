"""Narrow operator commands for generic non-trading workforce tasks."""
from __future__ import annotations

import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.command_api.security import enforce_command_rate_limit, verify_command_bearer
from app.command_api.service import CommandConflictError, execute_idempotent_command
from app.workers.models import WorkTaskAttemptRecord, WorkTaskRecord
from app.workers.registry import CAPABILITY_CATALOG
from app.workers.repository import (
    WorkforceRepositoryError,
    WorkerUnavailableError,
    cancel_task,
    enqueue_task,
    set_worker_accepting_tasks,
)

router = APIRouter(
    prefix="/api/v1/commands",
    tags=["workforce-commands"],
    dependencies=[Depends(verify_command_bearer), Depends(enforce_command_rate_limit)],
)

ALLOWED_WORKERS = frozenset({"supervisor", "haiku", "sonnet", "opus", "data_archivist"})
NON_TRADING_CAPABILITIES = frozenset({
    "instruction_intake", "task_decomposition", "workflow_supervision",
    "task_assignment_oversight", "result_review", "quick_research",
    "information_extraction", "summarization", "lightweight_classification",
    "data_analysis", "comparison", "evaluation", "structured_reasoning",
    "complex_reasoning", "strategic_analysis", "deep_review", "data_archiving",
    "artifact_indexing", "historical_retrieval_simulation", "data_organization",
})
ALLOWED_TASK_TYPES = frozenset(
    task_type
    for capability_id in NON_TRADING_CAPABILITIES
    for task_type in CAPABILITY_CATALOG[capability_id].task_types
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EnqueueTaskRequest(StrictRequest):
    task_type: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=180)
    required_capability: str = Field(min_length=2, max_length=64)
    requested_worker_id: str | None = Field(default=None, min_length=2, max_length=64)
    input_payload: dict = Field(default_factory=dict)
    priority: int = Field(default=50, ge=1, le=100)
    max_attempts: int = Field(default=2, ge=1, le=3)
    non_trading_acknowledged: Literal[True]

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("input_payload")
    @classmethod
    def validate_bounded_json_payload(cls, value: dict) -> dict:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise ValueError("input_payload exceeds the 16 KiB command limit.")
        return value

    @field_validator("requested_worker_id")
    @classmethod
    def validate_eligible_worker(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_WORKERS:
            raise ValueError("requested_worker_id is not eligible for non-trading commands.")
        return value

    @field_validator("required_capability")
    @classmethod
    def validate_non_trading_capability(cls, value: str) -> str:
        if value not in NON_TRADING_CAPABILITIES:
            raise ValueError("required_capability is not in the non-trading allowlist.")
        return value

    @field_validator("task_type")
    @classmethod
    def validate_non_trading_task_type(cls, value: str) -> str:
        if value not in ALLOWED_TASK_TYPES:
            raise ValueError("task_type is not in the non-trading allowlist.")
        return value

    @model_validator(mode="after")
    def validate_capability_pair(self) -> "EnqueueTaskRequest":
        if self.task_type not in CAPABILITY_CATALOG[self.required_capability].task_types:
            raise ValueError("task_type is not supported by required_capability.")
        return self


class TaskIdRequest(StrictRequest):
    task_id: str = Field(min_length=8, max_length=64)

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value):
            raise ValueError("task_id contains unsupported characters.")
        return value


class WorkerSchedulingRequest(StrictRequest):
    worker_id: str = Field(min_length=2, max_length=64)
    accepting_tasks: bool

    @field_validator("worker_id")
    @classmethod
    def validate_worker_id(cls, value: str) -> str:
        if value not in ALLOWED_WORKERS:
            raise ValueError("worker_id is not eligible for operator scheduling controls.")
        return value


def get_command_session(request: Request):
    """Resolve only the dedicated command session, never the read-only API session."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="Workforce command database is unavailable.")
    session: Session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _run(
    db: Session,
    *,
    command_type: str,
    idempotency_key: str,
    body: dict,
    executor,
) -> dict:
    try:
        with db.begin():
            return execute_idempotent_command(
                db,
                command_type=command_type,
                idempotency_key=idempotency_key,
                request_body=body,
                executor=executor,
            )
    except CommandConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkerUnavailableError as exc:
        raise HTTPException(status_code=409, detail="WORKER_UNAVAILABLE") from exc
    except WorkforceRepositoryError as exc:
        status = 404 if "not found" in str(exc).lower() else 409
        raise HTTPException(status_code=status, detail="WORKFORCE_COMMAND_REJECTED") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="COMMAND_VALIDATION_FAILED") from exc


def _idempotency_key(value: str | None) -> str:
    if value is None or not re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", value):
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key header is required.")
    return value


def _eligible_task(task: WorkTaskRecord) -> None:
    if task.required_capability not in NON_TRADING_CAPABILITIES or task.task_type not in ALLOWED_TASK_TYPES:
        raise HTTPException(status_code=403, detail="TASK_NOT_ELIGIBLE_FOR_OPERATOR_COMMANDS")


@router.post("/tasks/enqueue")
def enqueue_non_trading_task(
    request: EnqueueTaskRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_command_session),
) -> dict:
    key = _idempotency_key(idempotency_key)
    body = request.model_dump(mode="json")

    def execute(correlation_id: str):
        task, created = enqueue_task(
            db,
            task_type=request.task_type,
            title=request.title,
            required_capability=request.required_capability,
            input_payload=request.input_payload,
            idempotency_key=key,
            priority=request.priority,
            correlation_id=correlation_id,
            requested_worker_id=request.requested_worker_id,
            max_attempts=request.max_attempts,
        )
        return task.task_id, {
            "accepted": True, "created": created,
            "status": task.status, "task_id": task.task_id,
        }, task.correlation_id

    return _run(db, command_type="TASK_ENQUEUE", idempotency_key=key, body=body, executor=execute)


@router.post("/tasks/cancel")
def cancel_non_trading_task(
    request: TaskIdRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_command_session),
) -> dict:
    key = _idempotency_key(idempotency_key)
    body = request.model_dump(mode="json")

    def execute(_command_correlation_id: str):
        original = db.scalar(
            select(WorkTaskRecord).where(WorkTaskRecord.task_id == request.task_id).with_for_update()
        )
        if original is None:
            raise WorkforceRepositoryError("task not found")
        _eligible_task(original)
        task = cancel_task(db, task_id=request.task_id)
        return task.task_id, {
            "accepted": True,
            "status": task.status,
            "provider_outcome_unknown": task.status == "WAITING_REVIEW",
            "cancelled": task.status == "CANCELLED",
        }, task.correlation_id

    return _run(db, command_type="TASK_CANCEL", idempotency_key=key, body=body, executor=execute)


@router.post("/tasks/retry")
def retry_failed_non_trading_task(
    request: TaskIdRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_command_session),
) -> dict:
    key = _idempotency_key(idempotency_key)
    body = request.model_dump(mode="json")

    def execute(_command_correlation_id: str):
        original = db.scalar(
            select(WorkTaskRecord).where(WorkTaskRecord.task_id == request.task_id).with_for_update()
        )
        if original is None:
            raise WorkforceRepositoryError("task not found")
        _eligible_task(original)
        if (
            original.status != "FAILED"
            or original.payload_purged_at is not None
            or (original.requested_worker_id is not None and original.requested_worker_id not in ALLOWED_WORKERS)
        ):
            raise HTTPException(status_code=409, detail="TASK_NOT_SAFE_TO_RETRY")
        last_attempt = db.scalar(
            select(WorkTaskAttemptRecord)
            .where(WorkTaskAttemptRecord.task_id == original.task_id)
            .order_by(WorkTaskAttemptRecord.attempt_number.desc())
            .limit(1)
        )
        if (
            last_attempt is None
            or last_attempt.status != "FAILED"
            or last_attempt.provider_request_id is not None
            or last_attempt.attempt_number != original.attempts
        ):
            raise HTTPException(status_code=409, detail="TASK_PROVIDER_OUTCOME_NOT_CONFIRMED_SAFE")
        new_task, created = enqueue_task(
            db,
            task_type=original.task_type,
            title=("Retry: " + original.title)[:180],
            required_capability=original.required_capability,
            input_payload=dict(original.input_payload),
            idempotency_key=key,
            priority=original.priority,
            correlation_id=original.correlation_id,
            requested_worker_id=original.requested_worker_id,
            max_attempts=original.max_attempts,
        )
        return new_task.task_id, {
            "accepted": True, "created": created, "status": new_task.status,
            "task_id": new_task.task_id, "retry_of_task_id": original.task_id,
        }, original.correlation_id

    return _run(db, command_type="TASK_RETRY", idempotency_key=key, body=body, executor=execute)


@router.post("/workers/scheduling")
def set_worker_scheduling(
    request: WorkerSchedulingRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_command_session),
) -> dict:
    key = _idempotency_key(idempotency_key)
    body = request.model_dump(mode="json")

    def execute(correlation_id: str):
        worker = set_worker_accepting_tasks(
            db, worker_id=request.worker_id, accepting_tasks=request.accepting_tasks,
        )
        return worker.worker_id, {
            "accepted": True, "worker_id": worker.worker_id,
            "accepting_tasks": worker.accepting_tasks,
            "schedule_state": "ACCEPTING" if worker.accepting_tasks else "PAUSED",
        }, correlation_id

    return _run(db, command_type="WORKER_SCHEDULING_SET", idempotency_key=key, body=body, executor=execute)
