"""Pydantic response DTOs untuk Read-Only Observability API."""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

CurrencyCode = Literal["IDR", "USD"]


class RuntimeControlDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_mode: str
    trading_mode: str
    auto_execution_enabled: bool
    auto_paused: bool
    updated_at: datetime


class SystemStatusResponse(BaseModel):
    api_status: Literal["HEALTHY", "DEGRADED"] = "HEALTHY"
    database_status: Literal["CONNECTED", "DISCONNECTED"]
    server_time_utc: datetime
    runtime_control: RuntimeControlDTO | None = None
    process_monitoring_note: str = (
        "Worker OS liveness is unmonitored; persistent heartbeats are not available."
    )


class TokenBreakdownDTO(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0


class CurrencyTelemetryDTO(BaseModel):
    currency: CurrencyCode
    total_cost: Decimal
    total_reserved: Decimal
    total_projected: Decimal
    total_requests: int
    tokens: TokenBreakdownDTO
    status_counts: Dict[str, int] = Field(default_factory=dict)


class ModelTelemetryDTO(BaseModel):
    model: str
    tier: str
    currency: CurrencyCode
    requests: int = 0
    cost: Decimal
    reserved: Decimal
    projected: Decimal
    tokens: TokenBreakdownDTO


class TelemetryUsageResponse(BaseModel):
    period_start: date
    period_end: date
    period_days: int
    currencies: List[CurrencyTelemetryDTO]
    models: List[ModelTelemetryDTO]


class InFlightAnalysisDTO(BaseModel):
    task_id: int
    status: Literal["PROCESSING"] = "PROCESSING"
    liveness_status: Literal["IN_FLIGHT_UNCONFIRMED"] = "IN_FLIGHT_UNCONFIRMED"
    created_at: datetime
    elapsed_seconds: float = Field(ge=0)


class QueuedMessageDTO(BaseModel):
    queue_item_id: int
    status: Literal["PENDING", "CLAIMED"]
    queue_reason: Literal["BLOCKED_HARD_BUDGET", "MANUAL_REVIEW_BUDGET", "OTHER"]
    attempts: int = Field(ge=0)
    claimed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ActivePipelineResponse(BaseModel):
    server_time_utc: datetime
    total_in_flight: int
    total_queued: int
    in_flight_tasks: List[InFlightAnalysisDTO]
    queued_messages: List[QueuedMessageDTO]


class MarketWatchItemDTO(BaseModel):
    watch_id: int
    symbol: str
    status: Literal["WAITING_FOR_SPREAD", "RECHECKING"]
    spread_pips: Decimal | None = None
    maximum_spread_pips: Decimal
    last_bid: Decimal | None = None
    last_ask: Decimal | None = None
    check_count: int = Field(ge=0)
    last_checked_at: datetime | None = None
    next_check_at: datetime
    created_at: datetime


class ActiveMarketWatchesResponse(BaseModel):
    server_time_utc: datetime
    total_active_watches: int
    watches: List[MarketWatchItemDTO]


class AuditDecisionItemDTO(BaseModel):
    audit_id: int
    decision: Literal["APPROVED", "REJECTED"]
    previous_status: str
    new_status: str
    reviewer_role: Literal["Authorized Approver"] = "Authorized Approver"
    decided_at: datetime


class AuditDecisionsResponse(BaseModel):
    server_time_utc: datetime
    total_returned: int
    offset: int
    decisions: List[AuditDecisionItemDTO]


# Phase 4.4 workforce read-only DTOs. Never include task input/result or provider IDs.
WorkforceProcessStatus = Literal[
    "STARTING", "READY", "DRAINING", "STOPPED", "FAILED",
    "STALE", "OFFLINE", "DISABLED",
]
WorkforceTaskStatus = Literal[
    "QUEUED", "CLAIMED", "RUNNING", "RETRY_WAIT", "WAITING_REVIEW",
    "COMPLETED", "FAILED", "CANCELLED", "DEAD_LETTERED",
]
WorkforceAttemptStatus = Literal[
    "CLAIMED", "STARTED", "PROVIDER_REQUESTED", "COMPLETED",
    "FAILED", "TIMED_OUT", "UNKNOWN", "CANCELLED",
]
WorkforceActivityType = Literal[
    "TASK_ENQUEUED", "TASK_CLAIMED", "TASK_STARTED", "PROVIDER_REQUESTED",
    "TASK_RETRIED", "TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED",
    "TASK_WAITING_REVIEW", "TASK_DEAD_LETTERED", "WORKER_HEARTBEAT",
    "WORKER_STATE_CHANGED",
]


class WorkforceWorkerDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=2, max_length=64)
    display_name: str
    role: str
    department: str
    enabled: bool
    max_concurrency: int = Field(ge=1, le=32)
    model_tier: Literal["NONE", "FAST", "REASONING", "ESCALATION"]
    capability_ids: List[str] = Field(default_factory=list, max_length=28)
    process_status: WorkforceProcessStatus
    heartbeat_at: datetime | None = None
    active_task_count: int = Field(ge=0)
    in_flight_provider_requests: int = Field(ge=0)
    schema_version: Literal[1] = 1
    created_at: datetime
    updated_at: datetime
    accepting_tasks: bool


class WorkforceRuntimeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: datetime
    configured_workers: int = Field(ge=0)
    ready_workers: int = Field(ge=0)
    stale_workers: int = Field(ge=0)
    offline_workers: int = Field(ge=0)
    active_tasks: int = Field(ge=0)
    queued_tasks: int = Field(ge=0)
    waiting_review_tasks: int = Field(ge=0)
    dead_lettered_tasks: int = Field(ge=0)
    completed_last_24h: int = Field(ge=0)


class WorkforceRuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_time_utc: datetime
    runtime: WorkforceRuntimeDTO
    workers: List[WorkforceWorkerDTO]


class WorkforceTaskDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    correlation_id: str
    schema_version: Literal[1] = 1
    task_type: str
    title: str
    required_capability: str
    requested_worker_id: str | None = None
    assigned_worker_id: str | None = None
    priority: int = Field(ge=1, le=100)
    status: WorkforceTaskStatus
    attempts: int = Field(ge=0, le=100)
    max_attempts: int = Field(ge=1, le=10)
    not_before: datetime
    deadline_at: datetime | None = None
    lease_expires_at: datetime | None = None
    failure_code: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    related_task_id: str | None = None


class WorkforceTasksResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_time_utc: datetime
    total_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    tasks: List[WorkforceTaskDTO]


class WorkforceAttemptDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str
    task_id: str
    attempt_number: int = Field(ge=1, le=10)
    worker_id: str
    status: WorkforceAttemptStatus
    billing_currency: CurrencyCode | None = None
    cost_amount: Decimal | None = Field(default=None, ge=0)
    error_code: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class WorkforceAttemptsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_time_utc: datetime
    task_id: str
    attempts: List[WorkforceAttemptDTO]


class WorkforceActivityDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    event_type: WorkforceActivityType
    occurred_at: datetime
    worker_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    correlation_id: str
    event_seq: int | None = None
    actor_type: str = "LEGACY_UNKNOWN"
    actor_id: str | None = None
    related_task_id: str | None = None


class WorkforceActivityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_time_utc: datetime
    total_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    events: List[WorkforceActivityDTO]


class WorkforceTimelineEventDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_seq: int
    event_id: str
    task_id: str | None = None
    event_type: str
    occurred_at: datetime
    actor_type: str
    actor_id: str | None = None
    worker_id: str | None = None
    attempt_id: str | None = None
    attempt_number: int | None = None
    related_task_id: str | None = None
    correlation_id: str
    event_summary: dict = Field(default_factory=dict)


class WorkforceTaskSummaryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    status: str
    title: str
    task_type: str
    required_capability: str
    assigned_worker_id: str | None = None
    requested_worker_id: str | None = None
    priority: int
    attempts: int
    max_attempts: int
    created_at: datetime
    completed_at: datetime | None = None
    related_task_id: str | None = None


class WorkforceRetryLineageDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_of_task_id: str | None = None
    retried_by_task_id: str | None = None


class WorkforceTaskTimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_time_utc: datetime
    task_id: str
    task: WorkforceTaskSummaryDTO
    retry_lineage: WorkforceRetryLineageDTO
    total_events: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    events: List[WorkforceTimelineEventDTO]


class WorkforceReviewStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class WorkforceTaskReviewSummaryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str
    task_id: str
    attempt_id: str
    status: WorkforceReviewStatus
    original_output_sha256: str
    adjusted_output_sha256: str | None = None
    adjudicated_by: str | None = None
    adjudicated_at: datetime | None = None
    reason_code: str | None = None
    notes: str | None = None
    review_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    payload_purged_at: datetime | None = None


class WorkforceTaskReviewDetailDTO(WorkforceTaskReviewSummaryDTO):
    original_output: dict | None = None
    adjusted_output: dict | None = None


class WorkforceTaskReviewsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_time_utc: datetime
    total_count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    reviews: List[WorkforceTaskReviewSummaryDTO]
