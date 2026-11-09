"""Persistence models for the isolated generic workforce runtime (Phase 4)."""
from datetime import datetime, timezone
from decimal import Decimal

from enum import Enum
from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base

class ActorType(str, Enum):
    SYSTEM = "SYSTEM"
    WORKER = "WORKER"
    OPERATOR = "OPERATOR"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"


class WorkerDefinitionRecord(Base):
    __tablename__ = "workforce_workers"
    __table_args__ = (
        CheckConstraint("max_concurrency BETWEEN 1 AND 32", name="ck_workforce_worker_concurrency"),
    )

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(160), nullable=False)
    department: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accepting_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    model_tier: Mapped[str] = mapped_column(String(20), nullable=False, default="NONE")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class WorkerCapabilityRecord(Base):
    __tablename__ = "workforce_capabilities"
    capability_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    task_types: Mapped[list] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class WorkerCapabilityAssignmentRecord(Base):
    __tablename__ = "workforce_worker_capabilities"
    __table_args__ = (
        UniqueConstraint("worker_id", "capability_id", name="uq_workforce_worker_capability"),
    )
    worker_id: Mapped[str] = mapped_column(ForeignKey("workforce_workers.worker_id", ondelete="CASCADE"), primary_key=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("workforce_capabilities.capability_id", ondelete="RESTRICT"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkerHeartbeatRecord(Base):
    __tablename__ = "workforce_heartbeats"
    __table_args__ = (
        UniqueConstraint("worker_id", "instance_id", name="uq_workforce_heartbeat_instance"),
        CheckConstraint("active_task_count >= 0", name="ck_workforce_heartbeat_active_tasks"),
        CheckConstraint("in_flight_provider_requests >= 0", name="ck_workforce_heartbeat_provider_requests"),
        Index("ix_workforce_heartbeats_worker_time", "worker_id", "heartbeat_at"),
    )
    heartbeat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workforce_workers.worker_id", ondelete="CASCADE"), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(100), nullable=False)
    process_state: Mapped[str] = mapped_column(String(20), nullable=False)
    process_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    active_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_flight_provider_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class WorkTaskRecord(Base):
    __tablename__ = "workforce_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_workforce_task_idempotency"),
        CheckConstraint("priority BETWEEN 1 AND 100", name="ck_workforce_task_priority"),
        CheckConstraint("attempts >= 0 AND max_attempts BETWEEN 1 AND 10", name="ck_workforce_task_attempts"),
        CheckConstraint("status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'RETRY_WAIT', 'WAITING_REVIEW', 'COMPLETED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED')", name="ck_workforce_task_status"),
        CheckConstraint("payload_purged_at IS NULL OR (input_payload = '{}' AND result_payload IS NULL AND status IN ('WAITING_REVIEW', 'COMPLETED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED'))", name="ck_workforce_task_payload_purged_empty"),
        Index("ix_workforce_task_queue", "status", "not_before", "priority", "created_at"),
        Index("ix_workforce_task_worker_status", "assigned_worker_id", "status"),
    )
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    required_capability: Mapped[str] = mapped_column(ForeignKey("workforce_capabilities.capability_id", ondelete="RESTRICT"), nullable=False)
    requested_worker_id: Mapped[str | None] = mapped_column(ForeignKey("workforce_workers.worker_id", ondelete="SET NULL"), nullable=True)
    assigned_worker_id: Mapped[str | None] = mapped_column(ForeignKey("workforce_workers.worker_id", ondelete="SET NULL"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="QUEUED")
    input_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_payload: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    related_task_id: Mapped[str | None] = mapped_column(ForeignKey("workforce_tasks.task_id", ondelete="SET NULL"), nullable=True)


class WorkTaskAttemptRecord(Base):
    __tablename__ = "workforce_task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_workforce_task_attempt_number"),
        UniqueConstraint("provider_request_id", name="uq_workforce_provider_request_id"),
        CheckConstraint("attempt_number BETWEEN 1 AND 10", name="ck_workforce_attempt_number"),
        CheckConstraint("status IN ('CLAIMED', 'STARTED', 'PROVIDER_REQUESTED', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'UNKNOWN', 'CANCELLED')", name="ck_workforce_attempt_status"),
        Index("ix_workforce_attempt_task_created", "task_id", "created_at"),
    )
    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("workforce_tasks.task_id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workforce_workers.worker_id", ondelete="RESTRICT"), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="CLAIMED")
    provider_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class WorkActivityEventRecord(Base):
    __tablename__ = "workforce_activity_events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('SYSTEM', 'WORKER', 'OPERATOR', 'LEGACY_UNKNOWN')", name="ck_workforce_activity_actor_type"),
        Index("ix_workforce_activity_created", "occurred_at"),
        Index("ix_workforce_activity_task_created", "task_id", "occurred_at"),
        Index("ix_workforce_activity_task_seq", "task_id", "event_seq"),
    )
    event_seq: Mapped[int | None] = mapped_column(BigInteger, Identity(always=True), nullable=True)
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default=ActorType.LEGACY_UNKNOWN.value)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_task_id: Mapped[str | None] = mapped_column(ForeignKey("workforce_tasks.task_id", ondelete="SET NULL"), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)



class WorkforceCommandAuditRecord(Base):
    """Append-only evidence for authenticated, idempotent operator commands."""

    __tablename__ = "workforce_command_audit"
    __table_args__ = (
        UniqueConstraint("actor_id", "idempotency_key", name="uq_workforce_command_actor_idempotency"),
        CheckConstraint(
            "command_type IN ('TASK_ENQUEUE','TASK_CANCEL','TASK_RETRY','WORKER_SCHEDULING_SET','REVIEW_ADJUDICATE')",
            name="ck_workforce_command_type",
        ),
        CheckConstraint("length(request_fingerprint) = 64", name="ck_workforce_command_fingerprint"),
        Index("ix_workforce_command_audit_created", "created_at"),
        Index("ix_workforce_command_audit_correlation", "correlation_id"),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    command_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )



class WorkforceTaskReviewRecord(Base):
    """Permanent governance metadata record for human output reviews."""

    __tablename__ = "workforce_task_reviews"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_workforce_task_reviews_task"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'EDITED', 'REJECTED', 'EXPIRED')",
            name="ck_workforce_task_reviews_status",
        ),
        CheckConstraint(
            "length(original_output_sha256) = 64 AND (adjusted_output_sha256 IS NULL OR length(adjusted_output_sha256) = 64)",
            name="ck_workforce_task_reviews_sha256_format",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND adjudicated_at IS NULL AND adjudicated_by IS NULL) OR "
            "(status = 'EXPIRED' AND adjudicated_at IS NOT NULL AND adjudicated_by = 'system' AND reason_code = 'REVIEW_TIMED_OUT') OR "
            "(status IN ('APPROVED', 'EDITED', 'REJECTED') AND adjudicated_at IS NOT NULL AND adjudicated_by IS NOT NULL)",
            name="ck_workforce_task_reviews_adjudication",
        ),
        CheckConstraint(
            "(status = 'EDITED' AND adjusted_output_sha256 IS NOT NULL) OR (status != 'EDITED' AND adjusted_output_sha256 IS NULL)",
            name="ck_workforce_task_reviews_edited",
        ),
        CheckConstraint(
            "(status = 'REJECTED' AND reason_code IS NOT NULL) OR (status != 'REJECTED' AND (status != 'EXPIRED' OR reason_code = 'REVIEW_TIMED_OUT'))",
            name="ck_workforce_task_reviews_rejected",
        ),
        Index("ix_workforce_task_reviews_status_created", "status", "created_at"),
        Index("ix_workforce_task_reviews_expires", "review_expires_at"),
    )

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("workforce_tasks.task_id", ondelete="RESTRICT"), nullable=False, unique=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("workforce_task_attempts.attempt_id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    original_output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    adjusted_output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adjudicated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    adjudicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    review_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class WorkforceTaskReviewPayloadRecord(Base):
    """Payload storage table for human output reviews subject to retention purging."""

    __tablename__ = "workforce_task_review_payloads"
    __table_args__ = (
        CheckConstraint(
            "(payload_purged_at IS NULL AND original_output IS NOT NULL) OR "
            "(payload_purged_at IS NOT NULL AND original_output IS NULL AND adjusted_output IS NULL)",
            name="ck_workforce_review_payloads_purged",
        ),
    )

    review_id: Mapped[str] = mapped_column(ForeignKey("workforce_task_reviews.review_id", ondelete="RESTRICT"), primary_key=True)
    original_output: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    adjusted_output: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    payload_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
