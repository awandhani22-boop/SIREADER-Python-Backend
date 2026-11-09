"""Transactional repository for persistent generic workforce tasks.

Callers own the Session and transaction boundary. Functions flush but do not commit.
This module is deliberately separate from Telegram analysis and trading execution.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from json import dumps
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.workers.models import (
    WorkActivityEventRecord, WorkTaskAttemptRecord, WorkTaskRecord,
    WorkerCapabilityAssignmentRecord, WorkerCapabilityRecord,
    WorkerDefinitionRecord, WorkerHeartbeatRecord,
)
from app.workers.registry import CAPABILITY_CATALOG, build_default_registry
from app.workers.runtime_contract import (
    ActivityEventType, AttemptStatus, TaskStatus, WorkerHeartbeatContract,
    WorkerProcessState, validate_task_transition,
)

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_LEASE_SECONDS = 600
HEARTBEAT_TIMEOUT_SECONDS = 60
AGING_INTERVAL_MINUTES = 5


class WorkforceRepositoryError(RuntimeError):
    pass


class LeaseConflictError(WorkforceRepositoryError):
    pass


class WorkerUnavailableError(WorkforceRepositoryError):
    pass


def _now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _validate_payload(payload: dict | None) -> dict:
    payload = payload or {}
    encoded = dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return payload


def _append_event(db: Session, *, event_type: ActivityEventType, correlation_id: str,
                  worker_id: str | None = None, task_id: str | None = None,
                  attempt_id: str | None = None, payload: dict | None = None,
                  occurred_at: datetime | None = None) -> WorkActivityEventRecord:
    event = WorkActivityEventRecord(
        event_id=uuid4().hex, event_type=event_type.value, occurred_at=_now(occurred_at),
        worker_id=worker_id, task_id=task_id, attempt_id=attempt_id,
        correlation_id=correlation_id, payload=_validate_payload(payload),
    )
    db.add(event)
    return event


def sync_default_registry(db: Session) -> int:
    """Idempotently upsert the seven canonical definitions and 28 capability links."""
    registry = build_default_registry()
    now = datetime.now(timezone.utc)
    capability_ids = {capability_id for worker in registry.values() for capability_id in worker.capability_ids}
    existing_capabilities = {
        row.capability_id: row
        for row in db.execute(
            select(WorkerCapabilityRecord).where(WorkerCapabilityRecord.capability_id.in_(capability_ids))
        ).scalars()
    }
    for worker in registry.values():
        row = db.get(WorkerDefinitionRecord, worker.worker_id)
        values = {
            "display_name": worker.display_name, "role": worker.role,
            "department": worker.department,
            "max_concurrency": worker.max_concurrency, "model_tier": worker.model_tier,
            "schema_version": worker.schema_version, "updated_at": now,
        }
        if row is None:
            db.add(WorkerDefinitionRecord(worker_id=worker.worker_id, created_at=now, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        for capability_id in worker.capability_ids:
            capability = CAPABILITY_CATALOG[capability_id]
            cap_row = existing_capabilities.get(capability_id)
            cap_values = {
                "description": capability.description, "task_types": capability.task_types,
                "enabled": capability.enabled, "schema_version": capability.schema_version,
            }
            if cap_row is None:
                cap_row = WorkerCapabilityRecord(capability_id=capability_id, **cap_values)
                db.add(cap_row)
                existing_capabilities[capability_id] = cap_row
            else:
                cap_values.pop("enabled", None)
                for key, value in cap_values.items():
                    setattr(cap_row, key, value)
            link = db.get(WorkerCapabilityAssignmentRecord, (worker.worker_id, capability_id))
            if link is None:
                db.add(WorkerCapabilityAssignmentRecord(worker_id=worker.worker_id, capability_id=capability_id, enabled=True))
            # Preserve an operator's existing enabled/disabled override.
    db.flush()
    return len(registry)


def heartbeat_worker(db: Session, heartbeat: WorkerHeartbeatContract) -> WorkerHeartbeatRecord:
    """Upsert one worker process instance; older heartbeat timestamps are rejected."""
    worker = db.get(WorkerDefinitionRecord, heartbeat.worker_id)
    if worker is None or not worker.enabled:
        raise WorkerUnavailableError("worker is not registered or is disabled")
    row = db.scalar(
        select(WorkerHeartbeatRecord).where(
            WorkerHeartbeatRecord.worker_id == heartbeat.worker_id,
            WorkerHeartbeatRecord.instance_id == heartbeat.instance_id,
        ).with_for_update()
    )
    stamp = _now(heartbeat.heartbeat_at)
    if row is not None:
        current = _now(row.heartbeat_at)
        if stamp < current:
            raise WorkforceRepositoryError("heartbeat timestamp cannot move backwards")
        row.process_state = heartbeat.process_state.value
        row.process_started_at = _now(heartbeat.process_started_at)
        row.heartbeat_at = stamp
        row.active_task_count = heartbeat.active_task_count
        row.in_flight_provider_requests = heartbeat.in_flight_provider_requests
    else:
        row = WorkerHeartbeatRecord(
            heartbeat_id=uuid4().hex, worker_id=heartbeat.worker_id,
            instance_id=heartbeat.instance_id, process_state=heartbeat.process_state.value,
            process_started_at=_now(heartbeat.process_started_at), heartbeat_at=stamp,
            active_task_count=heartbeat.active_task_count,
            in_flight_provider_requests=heartbeat.in_flight_provider_requests,
        )
        db.add(row)
    db.flush()
    return row


def enqueue_task(db: Session, *, task_type: str, title: str, required_capability: str,
                 input_payload: dict, idempotency_key: str, priority: int = 50,
                 correlation_id: str | None = None, requested_worker_id: str | None = None,
                 max_attempts: int = 3, not_before: datetime | None = None,
                 deadline_at: datetime | None = None) -> tuple[WorkTaskRecord, bool]:
    """Create one generic task; a repeated idempotency key returns the original row."""
    if not isinstance(idempotency_key, str) or len(idempotency_key) < 8 or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain 8..128 characters")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 180:
        raise ValueError("title must contain 1..180 characters")
    if correlation_id is not None and (len(correlation_id) < 8 or len(correlation_id) > 64):
        raise ValueError("correlation_id must contain 8..64 characters")
    if required_capability not in CAPABILITY_CATALOG:
        raise ValueError("unknown required capability")
    if task_type not in CAPABILITY_CATALOG[required_capability].task_types:
        raise ValueError("task_type is not supported by required capability")
    if not 1 <= int(priority) <= 100 or not 1 <= int(max_attempts) <= 10:
        raise ValueError("priority or max_attempts is outside the allowed range")
    payload = _validate_payload(input_payload)
    now = datetime.now(timezone.utc)
    correlation_id = correlation_id or uuid4().hex
    existing = db.scalar(select(WorkTaskRecord).where(WorkTaskRecord.idempotency_key == idempotency_key))
    if existing is not None:
        return existing, False
    if requested_worker_id is not None:
        worker = db.get(WorkerDefinitionRecord, requested_worker_id)
        if worker is None or not worker.enabled:
            raise WorkerUnavailableError("requested worker is not registered or is disabled")
        linked = db.get(WorkerCapabilityAssignmentRecord, (requested_worker_id, required_capability))
        if linked is None or not linked.enabled:
            raise ValueError("requested worker does not have the required capability")
    row = WorkTaskRecord(
        task_id=uuid4().hex, idempotency_key=idempotency_key, correlation_id=correlation_id,
        schema_version=1, task_type=task_type, title=title.strip(),
        required_capability=required_capability, requested_worker_id=requested_worker_id,
        priority=int(priority), status=TaskStatus.QUEUED.value, input_payload=payload,
        attempts=0, max_attempts=int(max_attempts), not_before=_now(not_before or now),
        deadline_at=_now(deadline_at) if deadline_at else None,
        created_at=now, updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(WorkTaskRecord).where(WorkTaskRecord.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing, False
    _append_event(
        db, event_type=ActivityEventType.TASK_ENQUEUED, correlation_id=correlation_id,
        task_id=row.task_id, payload={"task_type": task_type, "required_capability": required_capability, "priority": priority},
        occurred_at=now,
    )
    db.flush()
    return row, True


def _require_fresh_worker_heartbeat(db: Session, worker_id: str, instance_id: str, now: datetime, timeout_seconds: int = HEARTBEAT_TIMEOUT_SECONDS) -> None:
    heartbeat = db.scalar(
        select(WorkerHeartbeatRecord).where(
            WorkerHeartbeatRecord.worker_id == worker_id,
            WorkerHeartbeatRecord.instance_id == instance_id,
        ).with_for_update()
    )
    if heartbeat is None or heartbeat.process_state != WorkerProcessState.READY.value:
        raise WorkerUnavailableError("worker process has no READY heartbeat")
    if _now(heartbeat.heartbeat_at) < now - timedelta(seconds=max(1, int(timeout_seconds))):
        raise WorkerUnavailableError("worker heartbeat is stale")


def claim_next_task(db: Session, *, worker_id: str, instance_id: str,
                    lease_seconds: int = 45, heartbeat_timeout_seconds: int = HEARTBEAT_TIMEOUT_SECONDS,
                    now: datetime | None = None) -> tuple[WorkTaskRecord, WorkTaskAttemptRecord] | None:
    """Atomically claim the highest effective-priority compatible task for one worker."""
    if not 5 <= int(lease_seconds) <= MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be between 5 and 600")
    stamp = _now(now)
    worker = db.scalar(select(WorkerDefinitionRecord).where(WorkerDefinitionRecord.worker_id == worker_id).with_for_update())
    if worker is None or not worker.enabled:
        raise WorkerUnavailableError("worker is not registered or is disabled")
    _require_fresh_worker_heartbeat(db, worker_id, instance_id, stamp, heartbeat_timeout_seconds)
    links = db.execute(
        select(WorkerCapabilityAssignmentRecord.capability_id).where(
            WorkerCapabilityAssignmentRecord.worker_id == worker_id,
            WorkerCapabilityAssignmentRecord.enabled.is_(True),
        )
    ).scalars().all()
    if not links:
        return None
    active_count = db.scalar(
        select(func.count(WorkTaskRecord.task_id)).where(
            WorkTaskRecord.assigned_worker_id == worker_id,
            WorkTaskRecord.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value]),
            WorkTaskRecord.lease_expires_at > stamp,
        )
    ) or 0
    if active_count >= worker.max_concurrency:
        return None
    age_bonus = func.floor(
        func.extract("epoch", stamp - WorkTaskRecord.created_at) / (AGING_INTERVAL_MINUTES * 60)
    )
    effective_priority = func.least(100, WorkTaskRecord.priority + age_bonus)
    task = db.execute(
        select(WorkTaskRecord).where(
            WorkTaskRecord.status.in_([TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value]),
            WorkTaskRecord.required_capability.in_(links),
            WorkTaskRecord.not_before <= stamp,
            (WorkTaskRecord.deadline_at.is_(None) | (WorkTaskRecord.deadline_at > stamp)),
            (WorkTaskRecord.requested_worker_id.is_(None) | (WorkTaskRecord.requested_worker_id == worker_id)),
        ).order_by(effective_priority.desc(), WorkTaskRecord.created_at.asc(), WorkTaskRecord.task_id.asc())
        .limit(1).with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if task is None:
        return None
    current_status = TaskStatus(task.status)
    if current_status is TaskStatus.RETRY_WAIT:
        validate_task_transition(TaskStatus.RETRY_WAIT, TaskStatus.QUEUED)
        task.status = TaskStatus.QUEUED.value
    validate_task_transition(TaskStatus(task.status), TaskStatus.CLAIMED)
    token = uuid4().hex
    task.status = TaskStatus.CLAIMED.value
    task.assigned_worker_id = worker_id
    task.attempts = int(task.attempts or 0) + 1
    task.lease_token = token
    task.lease_owner = instance_id
    task.lease_expires_at = stamp + timedelta(seconds=int(lease_seconds))
    task.updated_at = stamp
    attempt = WorkTaskAttemptRecord(
        attempt_id=uuid4().hex, task_id=task.task_id, attempt_number=task.attempts,
        worker_id=worker_id, lease_token=token, status=AttemptStatus.CLAIMED.value,
        created_at=stamp,
    )
    db.add(attempt)
    _append_event(
        db, event_type=ActivityEventType.TASK_CLAIMED, correlation_id=task.correlation_id,
        worker_id=worker_id, task_id=task.task_id, attempt_id=attempt.attempt_id,
        payload={"attempt_number": attempt.attempt_number}, occurred_at=stamp,
    )
    db.flush()
    return task, attempt


def _locked_task_attempt(db: Session, task_id: str, lease_token: str, now: datetime | None = None) -> tuple[WorkTaskRecord, WorkTaskAttemptRecord]:
    task = db.scalar(select(WorkTaskRecord).where(WorkTaskRecord.task_id == task_id).with_for_update())
    attempt = db.scalar(select(WorkTaskAttemptRecord).where(WorkTaskAttemptRecord.lease_token == lease_token).with_for_update())
    if task is None or attempt is None or attempt.task_id != task_id:
        raise LeaseConflictError("task lease or attempt was not found")
    if task.lease_token != lease_token or task.status not in (TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value):
        raise LeaseConflictError("task lease is no longer owned by this attempt")
    if task.lease_expires_at is None or _now(task.lease_expires_at) <= _now(now):
        raise LeaseConflictError("task lease has expired")
    return task, attempt


def start_task(db: Session, *, task_id: str, lease_token: str, now: datetime | None = None) -> WorkTaskRecord:
    task, attempt = _locked_task_attempt(db, task_id, lease_token, now)
    validate_task_transition(TaskStatus(task.status), TaskStatus.RUNNING)
    stamp = _now(now)
    task.status = TaskStatus.RUNNING.value
    task.updated_at = stamp
    attempt.status = AttemptStatus.STARTED.value
    attempt.started_at = stamp
    _append_event(db, event_type=ActivityEventType.TASK_STARTED, correlation_id=task.correlation_id, worker_id=attempt.worker_id, task_id=task.task_id, attempt_id=attempt.attempt_id, occurred_at=stamp)
    db.flush()
    return task


def mark_provider_requested(db: Session, *, task_id: str, lease_token: str, provider_request_id: str | None = None, now: datetime | None = None) -> WorkTaskAttemptRecord:
    task, attempt = _locked_task_attempt(db, task_id, lease_token, now)
    if task.status != TaskStatus.RUNNING.value or attempt.status != AttemptStatus.STARTED.value:
        raise LeaseConflictError("task must be RUNNING before provider request")
    stamp = _now(now)
    stable_provider_request_id = provider_request_id or attempt.attempt_id
    if not isinstance(stable_provider_request_id, str) or not stable_provider_request_id or len(stable_provider_request_id) > 64:
        raise ValueError("provider_request_id must contain 1..64 characters")
    attempt.status = AttemptStatus.PROVIDER_REQUESTED.value
    attempt.provider_request_id = stable_provider_request_id
    attempt.provider_started_at = stamp
    _append_event(db, event_type=ActivityEventType.PROVIDER_REQUESTED, correlation_id=task.correlation_id, worker_id=attempt.worker_id, task_id=task.task_id, attempt_id=attempt.attempt_id, payload={"provider_request_id": stable_provider_request_id}, occurred_at=stamp)
    db.flush()
    return attempt


def renew_task_lease(db: Session, *, task_id: str, lease_token: str, lease_seconds: int = 45, now: datetime | None = None) -> datetime:
    if not 5 <= int(lease_seconds) <= MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be between 5 and 600")
    task, _ = _locked_task_attempt(db, task_id, lease_token, now)
    expiry = _now(now) + timedelta(seconds=int(lease_seconds))
    task.lease_expires_at = expiry
    task.updated_at = _now(now)
    db.flush()
    return expiry


def complete_task(db: Session, *, task_id: str, lease_token: str, result_payload: dict,
                  billing_currency: str | None = None, cost_amount: str | None = None,
                  now: datetime | None = None) -> WorkTaskRecord:
    task, attempt = _locked_task_attempt(db, task_id, lease_token, now)
    payload = _validate_payload(result_payload)
    stamp = _now(now)
    validate_task_transition(TaskStatus(task.status), TaskStatus.COMPLETED)
    task.status = TaskStatus.COMPLETED.value
    task.result_payload = payload
    task.completed_at = stamp
    task.updated_at = stamp
    task.lease_token = None
    task.lease_owner = None
    task.lease_expires_at = None
    attempt.status = AttemptStatus.COMPLETED.value
    attempt.finished_at = stamp
    if billing_currency is not None:
        if billing_currency not in ("IDR", "USD"):
            raise ValueError("invalid billing currency")
        attempt.billing_currency = billing_currency
    if cost_amount is not None:
        from decimal import Decimal
        amount = Decimal(str(cost_amount))
        if amount < 0:
            raise ValueError("cost_amount cannot be negative")
        attempt.cost_amount = amount
    _append_event(db, event_type=ActivityEventType.TASK_COMPLETED, correlation_id=task.correlation_id, worker_id=attempt.worker_id, task_id=task.task_id, attempt_id=attempt.attempt_id, payload={"source": payload.get("source", "LIVE")}, occurred_at=stamp)
    db.flush()
    return task


def fail_task(db: Session, *, task_id: str, lease_token: str, error_code: str,
              retryable: bool = False, outcome_unknown: bool = False,
              backoff_seconds: int = 30, now: datetime | None = None) -> WorkTaskRecord:
    task, attempt = _locked_task_attempt(db, task_id, lease_token, now)
    stamp = _now(now)
    safe_code = (error_code or "WORKER_FAILED")[:80]
    provider_was_requested = bool(attempt.provider_request_id) or attempt.status == AttemptStatus.PROVIDER_REQUESTED.value
    task.failure_code = safe_code
    task.updated_at = stamp
    attempt.error_code = safe_code
    attempt.finished_at = stamp
    if outcome_unknown or provider_was_requested:
        validate_task_transition(TaskStatus(task.status), TaskStatus.WAITING_REVIEW)
        task.status = TaskStatus.WAITING_REVIEW.value
        attempt.status = AttemptStatus.UNKNOWN.value
        event = ActivityEventType.TASK_WAITING_REVIEW
    elif retryable and task.attempts < task.max_attempts:
        validate_task_transition(TaskStatus(task.status), TaskStatus.RETRY_WAIT)
        delay = min(3600, max(1, int(backoff_seconds)))
        task.status = TaskStatus.RETRY_WAIT.value
        task.not_before = stamp + timedelta(seconds=delay)
        attempt.status = AttemptStatus.FAILED.value
        event = ActivityEventType.TASK_RETRIED
    elif task.attempts >= task.max_attempts:
        validate_task_transition(TaskStatus(task.status), TaskStatus.DEAD_LETTERED)
        task.status = TaskStatus.DEAD_LETTERED.value
        attempt.status = AttemptStatus.FAILED.value
        event = ActivityEventType.TASK_DEAD_LETTERED
    else:
        validate_task_transition(TaskStatus(task.status), TaskStatus.FAILED)
        task.status = TaskStatus.FAILED.value
        attempt.status = AttemptStatus.FAILED.value
        event = ActivityEventType.TASK_FAILED
    task.lease_token = None
    task.lease_owner = None
    task.lease_expires_at = None
    _append_event(db, event_type=event, correlation_id=task.correlation_id, worker_id=attempt.worker_id, task_id=task.task_id, attempt_id=attempt.attempt_id, payload={"error_code": safe_code, "retryable": retryable, "outcome_unknown": outcome_unknown or provider_was_requested}, occurred_at=stamp)
    db.flush()
    return task


def cancel_task(db: Session, *, task_id: str, now: datetime | None = None) -> WorkTaskRecord:
    task = db.scalar(select(WorkTaskRecord).where(WorkTaskRecord.task_id == task_id).with_for_update())
    if task is None:
        raise WorkforceRepositoryError("task not found")
    status = TaskStatus(task.status)
    if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DEAD_LETTERED):
        return task
    stamp = _now(now)
    active_attempt = None
    if task.lease_token:
        active_attempt = db.scalar(select(WorkTaskAttemptRecord).where(WorkTaskAttemptRecord.lease_token == task.lease_token).with_for_update())
    provider_was_requested = bool(active_attempt and (active_attempt.provider_request_id or active_attempt.status == AttemptStatus.PROVIDER_REQUESTED.value))
    if provider_was_requested:
        validate_task_transition(status, TaskStatus.WAITING_REVIEW)
        task.status = TaskStatus.WAITING_REVIEW.value
        active_attempt.status = AttemptStatus.UNKNOWN.value
        active_attempt.error_code = "CANCEL_AFTER_PROVIDER_REQUEST"
        active_attempt.finished_at = stamp
        event_type = ActivityEventType.TASK_WAITING_REVIEW
    else:
        validate_task_transition(status, TaskStatus.CANCELLED)
        task.status = TaskStatus.CANCELLED.value
        if active_attempt is not None:
            active_attempt.status = AttemptStatus.CANCELLED.value
            active_attempt.finished_at = stamp
        event_type = ActivityEventType.TASK_CANCELLED
    task.updated_at = stamp
    task.lease_token = None
    task.lease_owner = None
    task.lease_expires_at = None
    _append_event(db, event_type=event_type, correlation_id=task.correlation_id, worker_id=task.assigned_worker_id, task_id=task.task_id, attempt_id=active_attempt.attempt_id if active_attempt else None, payload={"cancelled": True, "provider_outcome_unknown": provider_was_requested}, occurred_at=stamp)
    db.flush()
    return task


def recover_expired_leases(db: Session, *, now: datetime | None = None, base_backoff_seconds: int = 30) -> dict[str, int]:
    """Recover expired work without ever blindly replaying a possibly sent provider request."""
    stamp = _now(now)
    rows = db.execute(
        select(WorkTaskRecord).where(
            WorkTaskRecord.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value]),
            WorkTaskRecord.lease_expires_at.is_not(None),
            WorkTaskRecord.lease_expires_at <= stamp,
        ).order_by(WorkTaskRecord.created_at.asc()).with_for_update(skip_locked=True)
    ).scalars().all()
    recovered = {"requeued": 0, "waiting_review": 0, "dead_lettered": 0}
    for task in rows:
        attempt = db.scalar(select(WorkTaskAttemptRecord).where(WorkTaskAttemptRecord.lease_token == task.lease_token).with_for_update())
        provider_was_requested = bool(attempt and (attempt.provider_request_id or attempt.status == AttemptStatus.PROVIDER_REQUESTED.value))
        if provider_was_requested:
            validate_task_transition(TaskStatus(task.status), TaskStatus.WAITING_REVIEW)
            task.status = TaskStatus.WAITING_REVIEW.value
            attempt.status = AttemptStatus.UNKNOWN.value
            attempt.error_code = "LEASE_EXPIRED_AFTER_PROVIDER_REQUEST"
            recovered["waiting_review"] += 1
            event = ActivityEventType.TASK_WAITING_REVIEW
        elif task.attempts < task.max_attempts and (task.deadline_at is None or _now(task.deadline_at) > stamp):
            validate_task_transition(TaskStatus(task.status), TaskStatus.RETRY_WAIT)
            task.status = TaskStatus.RETRY_WAIT.value
            task.not_before = stamp + timedelta(seconds=min(3600, max(1, base_backoff_seconds * (2 ** max(0, task.attempts - 1)))))
            if attempt is not None:
                attempt.status = AttemptStatus.TIMED_OUT.value
                attempt.error_code = "LEASE_EXPIRED_BEFORE_PROVIDER"
            recovered["requeued"] += 1
            event = ActivityEventType.TASK_RETRIED
        else:
            validate_task_transition(TaskStatus(task.status), TaskStatus.DEAD_LETTERED)
            task.status = TaskStatus.DEAD_LETTERED.value
            if attempt is not None:
                attempt.status = AttemptStatus.TIMED_OUT.value
                attempt.error_code = "LEASE_EXPIRED_MAX_ATTEMPTS"
            recovered["dead_lettered"] += 1
            event = ActivityEventType.TASK_DEAD_LETTERED
        task.failure_code = attempt.error_code if attempt is not None else "LEASE_EXPIRED"
        task.lease_token = None
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = stamp
        _append_event(db, event_type=event, correlation_id=task.correlation_id, worker_id=task.assigned_worker_id, task_id=task.task_id, attempt_id=attempt.attempt_id if attempt else None, payload={"provider_outcome_unknown": provider_was_requested}, occurred_at=stamp)
    db.flush()
    return recovered



def expire_deadline_tasks(db: Session, *, now: datetime | None = None) -> int:
    """Dead-letter queued/retry work whose deadline elapsed before execution."""
    stamp = _now(now)
    rows = db.execute(
        select(WorkTaskRecord).where(
            WorkTaskRecord.status.in_([TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value]),
            WorkTaskRecord.deadline_at.is_not(None),
            WorkTaskRecord.deadline_at <= stamp,
        ).order_by(WorkTaskRecord.deadline_at.asc()).with_for_update(skip_locked=True)
    ).scalars().all()
    for task in rows:
        validate_task_transition(TaskStatus(task.status), TaskStatus.DEAD_LETTERED)
        task.status = TaskStatus.DEAD_LETTERED.value
        task.failure_code = "DEADLINE_EXPIRED"
        task.updated_at = stamp
        _append_event(
            db, event_type=ActivityEventType.TASK_DEAD_LETTERED,
            correlation_id=task.correlation_id, task_id=task.task_id,
            payload={"error_code": "DEADLINE_EXPIRED"}, occurred_at=stamp,
        )
    db.flush()
    return len(rows)
