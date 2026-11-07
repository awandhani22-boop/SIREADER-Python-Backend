"""Pydantic response DTOs untuk Read-Only Observability API."""
from datetime import date, datetime
from decimal import Decimal
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
