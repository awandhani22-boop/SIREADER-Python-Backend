import pytest
from pydantic import ValidationError

from app.workers.runtime_contract import (
    ActivityEventContract, ActivityEventType, AttemptStatus, TaskAttemptContract,
    TaskContract, TaskStatus, WorkerContract, WorkerHeartbeatContract,
    WorkerProcessState, is_terminal_task_status, validate_task_transition,
    InvalidTaskTransition, MAX_TASK_PAYLOAD_BYTES,
)
from app.workers.registry import (
    CAPABILITY_CATALOG, build_default_registry, eligible_workers, validate_registry,
)
from datetime import datetime, timezone

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def test_default_registry_matches_existing_seven_worker_ids_and_order():
    registry = build_default_registry()
    assert list(registry) == [
        "supervisor", "haiku", "sonnet", "opus",
        "decision_engineer", "market_monitor", "data_archivist",
    ]
    assert len(CAPABILITY_CATALOG) == 28
    assert registry["haiku"].model_tier == "FAST"
    assert registry["sonnet"].model_tier == "REASONING"
    assert registry["opus"].model_tier == "ESCALATION"
    assert registry["market_monitor"].max_concurrency == 1


def test_capability_resolution_is_deterministic_and_rejects_unknown():
    assert [w.worker_id for w in eligible_workers("decision_evaluation")] == [
        "opus", "decision_engineer",
    ]
    with pytest.raises(ValueError, match="unknown capability"):
        eligible_workers("not-a-capability")


def test_worker_registry_rejects_key_mismatch_and_unknown_capability():
    registry = build_default_registry()
    with pytest.raises(ValueError, match="registry key"):
        validate_registry({"wrong": registry["haiku"]})
    broken = registry["haiku"].model_copy(update={"capability_ids": ["ghost"]})
    with pytest.raises(ValueError, match="unknown capabilities"):
        validate_registry({"haiku": broken})


def test_contracts_reject_extra_fields_and_bad_versions():
    base = build_default_registry()["haiku"].model_dump()
    with pytest.raises(ValidationError):
        WorkerContract(**{**base, "schema_version": 2})
    with pytest.raises(ValidationError):
        WorkerContract(**{**base, "api_key": "must-never-be-a-contract-field"})


def test_task_payload_is_bounded_and_json_only():
    common = dict(
        task_id="task_12345678", idempotency_key="idem_12345678",
        correlation_id="corr_12345678", task_type="SUMMARIZATION",
        title="Summarize release notes", required_capability="summarization",
        priority=50, status=TaskStatus.QUEUED, attempts=0, max_attempts=3,
        created_at=NOW, updated_at=NOW,
    )
    TaskContract(**common, input_payload={"text": "hello"})
    with pytest.raises(ValidationError, match="exceeds"):
        TaskContract(**common, input_payload={"text": "x" * (MAX_TASK_PAYLOAD_BYTES + 1)})
    with pytest.raises(ValidationError, match="valid JSON"):
        TaskContract(**common, input_payload={"bad": object()})


def test_task_lifecycle_rejects_invalid_and_terminal_transitions():
    validate_task_transition(TaskStatus.QUEUED, TaskStatus.CLAIMED)
    validate_task_transition(TaskStatus.RUNNING, TaskStatus.WAITING_REVIEW)
    with pytest.raises(InvalidTaskTransition):
        validate_task_transition(TaskStatus.COMPLETED, TaskStatus.QUEUED)
    assert is_terminal_task_status(TaskStatus.DEAD_LETTERED)
    assert not is_terminal_task_status(TaskStatus.RETRY_WAIT)


def test_heartbeat_keeps_process_and_task_metrics_separate():
    heartbeat = WorkerHeartbeatContract(
        worker_id="haiku", instance_id="instance-12345678",
        process_state=WorkerProcessState.READY, process_started_at=NOW,
        heartbeat_at=NOW, active_task_count=0, in_flight_provider_requests=0,
        observed_at=NOW,
    )
    assert heartbeat.process_state is WorkerProcessState.READY
    assert heartbeat.active_task_count == 0
    assert heartbeat.in_flight_provider_requests == 0


def test_attempt_and_activity_contract_are_versioned():
    attempt = TaskAttemptContract(
        attempt_id="attempt_12345678", task_id="task_12345678",
        attempt_number=1, worker_id="haiku", status=AttemptStatus.CLAIMED,
    )
    event = ActivityEventContract(
        event_id="event_12345678", event_type=ActivityEventType.TASK_CLAIMED,
        occurred_at=NOW, worker_id="haiku", task_id=attempt.task_id,
        attempt_id=attempt.attempt_id, correlation_id="corr_12345678",
        payload={"attempt_number": 1},
    )
    assert attempt.schema_version == 1
    assert event.schema_version == 1
