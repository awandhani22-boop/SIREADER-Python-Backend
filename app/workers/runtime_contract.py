"""Versioned, UI/provider-independent contracts for the SIREADER worker runtime."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from json import dumps
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = 1
MAX_TASK_PAYLOAD_BYTES = 64 * 1024
MAX_EVENT_PAYLOAD_BYTES = 16 * 1024


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class WorkerProcessState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    WAITING_REVIEW = "WAITING_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEAD_LETTERED = "DEAD_LETTERED"


class AttemptStatus(StrEnum):
    CLAIMED = "CLAIMED"
    STARTED = "STARTED"
    PROVIDER_REQUESTED = "PROVIDER_REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class ActivityEventType(StrEnum):
    TASK_ENQUEUED = "TASK_ENQUEUED"
    TASK_CLAIMED = "TASK_CLAIMED"
    TASK_STARTED = "TASK_STARTED"
    PROVIDER_REQUESTED = "PROVIDER_REQUESTED"
    TASK_RETRIED = "TASK_RETRIED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_WAITING_REVIEW = "TASK_WAITING_REVIEW"
    TASK_DEAD_LETTERED = "TASK_DEAD_LETTERED"
    WORKER_HEARTBEAT = "WORKER_HEARTBEAT"
    WORKER_STATE_CHANGED = "WORKER_STATE_CHANGED"


WorkerId = str
CapabilityId = str
TaskId = str


class CapabilityContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    capability_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    description: str = Field(min_length=3, max_length=300)
    task_types: list[str] = Field(min_length=1, max_length=20)
    enabled: bool = True

    @field_validator("task_types")
    @classmethod
    def unique_task_types(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("task_types must be unique")
        if any(not item or len(item) > 64 for item in value):
            raise ValueError("task_types contain an invalid value")
        return value


class WorkerContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    worker_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    display_name: str = Field(min_length=2, max_length=100)
    role: str = Field(min_length=3, max_length=160)
    department: str = Field(min_length=2, max_length=80)
    capability_ids: list[str] = Field(max_length=40)
    enabled: bool = True
    max_concurrency: int = Field(ge=1, le=32)
    model_tier: Literal["NONE", "FAST", "REASONING", "ESCALATION"] = "NONE"

    @field_validator("capability_ids")
    @classmethod
    def unique_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capability_ids must be unique")
        return value


class WorkerHeartbeatContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    worker_id: str = Field(min_length=2, max_length=64)
    instance_id: str = Field(min_length=8, max_length=100)
    process_state: WorkerProcessState
    process_started_at: datetime
    heartbeat_at: datetime
    active_task_count: int = Field(ge=0, le=1000)
    in_flight_provider_requests: int = Field(ge=0, le=1000)
    observed_at: datetime


class RuntimeHealthContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    observed_at: datetime
    heartbeat_timeout_seconds: int = Field(ge=1, le=3600)
    worker_count_configured: int = Field(ge=0, le=1000)
    worker_count_ready: int = Field(ge=0, le=1000)
    active_task_count: int = Field(ge=0, le=100000)
    queued_task_count: int = Field(ge=0, le=100000)
    uncertain_task_count: int = Field(ge=0, le=100000)


class TaskContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    task_id: str = Field(min_length=8, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)
    correlation_id: str = Field(min_length=8, max_length=64)
    task_type: str = Field(min_length=2, max_length=64, pattern=r"^[A-Z][A-Z0-9_]+$")
    title: str = Field(min_length=2, max_length=180)
    required_capability: str = Field(min_length=2, max_length=64)
    priority: int = Field(ge=1, le=100)
    status: TaskStatus
    assigned_worker_id: str | None = Field(default=None, max_length=64)
    attempts: int = Field(ge=0, le=100)
    max_attempts: int = Field(ge=1, le=10)
    created_at: datetime
    updated_at: datetime
    not_before: datetime | None = None
    deadline_at: datetime | None = None
    input_payload: dict[str, object] = Field(default_factory=dict)
    result_payload: dict[str, object] | None = None

    @field_validator("input_payload", "result_payload")
    @classmethod
    def bounded_json(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is None:
            return value
        try:
            encoded = dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be valid JSON") from exc
        if len(encoded) > MAX_TASK_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_TASK_PAYLOAD_BYTES} bytes")
        return value


class TaskAttemptContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    attempt_id: str = Field(min_length=8, max_length=64)
    task_id: str = Field(min_length=8, max_length=64)
    attempt_number: int = Field(ge=1, le=10)
    worker_id: str = Field(min_length=2, max_length=64)
    status: AttemptStatus
    provider_request_id: str | None = Field(default=None, max_length=64)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    billing_currency: Literal["IDR", "USD"] | None = None
    cost_amount: str | None = Field(default=None, max_length=32)
    error_code: str | None = Field(default=None, max_length=80)


class ActivityEventContract(ContractModel):
    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    event_id: str = Field(min_length=8, max_length=64)
    event_type: ActivityEventType
    occurred_at: datetime
    worker_id: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    attempt_id: str | None = Field(default=None, max_length=64)
    correlation_id: str = Field(min_length=8, max_length=64)
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def bounded_event_payload(cls, value: dict[str, object]) -> dict[str, object]:
        try:
            encoded = dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("event payload must be valid JSON") from exc
        if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError(f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes")
        return value


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.CLAIMED, TaskStatus.CANCELLED, TaskStatus.DEAD_LETTERED}),
    TaskStatus.CLAIMED: frozenset({TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.RETRY_WAIT, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.WAITING_REVIEW, TaskStatus.DEAD_LETTERED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.RETRY_WAIT, TaskStatus.CANCELLED, TaskStatus.WAITING_REVIEW, TaskStatus.DEAD_LETTERED}),
    TaskStatus.RETRY_WAIT: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.DEAD_LETTERED}),
    TaskStatus.WAITING_REVIEW: frozenset({TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.DEAD_LETTERED: frozenset(),
}


class InvalidTaskTransition(ValueError):
    pass


def validate_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Raise when the requested lifecycle transition is not explicitly allowed."""
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidTaskTransition(f"Invalid task transition: {current.value} -> {target.value}")


def is_terminal_task_status(status: TaskStatus) -> bool:
    return not TASK_TRANSITIONS[status]


def utc_now() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)
