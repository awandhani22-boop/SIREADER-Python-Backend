"""Phase 5.1 workforce command API security and lifecycle tests (SQLite only)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.command_api.server import create_command_app
from app.database.models import Base
from app.workers.models import (
    WorkActivityEventRecord,
    WorkTaskAttemptRecord,
    WorkTaskRecord,
    WorkerCapabilityAssignmentRecord,
    WorkerCapabilityRecord,
    WorkerDefinitionRecord,
    WorkerHeartbeatRecord,
    WorkforceCommandAuditRecord,
)
from app.workers.repository import enqueue_task, sync_default_registry, claim_next_task

TOKEN = "phase5-command-test-token-0123456789"
AUTH = {"Authorization": "Bearer " + TOKEN}
TABLES = [
    WorkerDefinitionRecord.__table__,
    WorkerCapabilityRecord.__table__,
    WorkerCapabilityAssignmentRecord.__table__,
    WorkerHeartbeatRecord.__table__,
    WorkTaskRecord.__table__,
    WorkTaskAttemptRecord.__table__,
    WorkActivityEventRecord.__table__,
    WorkforceCommandAuditRecord.__table__,
]


@pytest.fixture()
def command_environment():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=TABLES)
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with Session.begin() as db:
        sync_default_registry(db)
    app = create_command_app(session_factory=Session, token=TOKEN, validate_database=False)
    with TestClient(app) as client:
        yield client, Session
    engine.dispose()


def _enqueue_body(**overrides):
    body = {
        "task_type": "SUMMARIZATION",
        "title": "Summarize approved research notes",
        "required_capability": "summarization",
        "requested_worker_id": "haiku",
        "input_payload": {"text": "safe test content"},
        "priority": 50,
        "max_attempts": 2,
        "non_trading_acknowledged": True,
    }
    body.update(overrides)
    return body


def _seed_task(
    Session,
    *,
    task_id: str,
    idempotency_key: str,
    status: str,
    attempts: int = 1,
    lease_token: str | None = None,
    provider_request_id: str | None = None,
    attempt_status: str = "FAILED",
    payload_purged_at=None,
):
    now = datetime.now(timezone.utc)
    with Session.begin() as db:
        task = WorkTaskRecord(
            task_id=task_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-" + task_id,
            schema_version=1,
            task_type="SUMMARIZATION",
            title="Seeded safe task",
            required_capability="summarization",
            requested_worker_id="haiku",
            assigned_worker_id="haiku" if lease_token else None,
            priority=50,
            status=status,
            input_payload={} if payload_purged_at else {"text": "safe test"},
            result_payload=None,
            attempts=attempts,
            max_attempts=2,
            not_before=now,
            deadline_at=None,
            lease_token=lease_token,
            lease_owner="test-instance" if lease_token else None,
            lease_expires_at=now + timedelta(minutes=1) if lease_token else None,
            failure_code=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            payload_expires_at=None,
            payload_purged_at=payload_purged_at,
        )
        db.add(task)
        if attempts:
            db.add(WorkTaskAttemptRecord(
                attempt_id="attempt-" + task_id,
                task_id=task_id,
                attempt_number=attempts,
                worker_id="haiku",
                lease_token=lease_token or ("lease-" + task_id),
                status=attempt_status,
                provider_request_id=provider_request_id,
                provider_started_at=now if provider_request_id else None,
                started_at=now,
                finished_at=now if attempt_status in {"FAILED", "PROVIDER_REQUESTED"} else None,
                created_at=now,
            ))
    return task_id


def test_commands_require_dedicated_bearer_before_side_effects(command_environment):
    client, Session = command_environment
    response = client.post(
        "/api/v1/commands/tasks/enqueue",
        json=_enqueue_body(),
        headers={"Idempotency-Key": "command-auth-001"},
    )
    assert response.status_code == 401
    with Session() as db:
        assert db.scalar(select(WorkTaskRecord)) is None
        assert db.scalar(select(WorkforceCommandAuditRecord)) is None


def test_enqueue_is_audited_and_same_key_replays_without_duplicate(command_environment):
    client, Session = command_environment
    headers = {**AUTH, "Idempotency-Key": "command-enqueue-001"}
    first = client.post("/api/v1/commands/tasks/enqueue", json=_enqueue_body(), headers=headers)
    replay = client.post("/api/v1/commands/tasks/enqueue", json=_enqueue_body(), headers=headers)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["target_id"] == replay.json()["target_id"]
    assert first.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    with Session() as db:
        assert len(db.scalars(select(WorkTaskRecord)).all()) == 1
        audits = db.scalars(select(WorkforceCommandAuditRecord)).all()
        assert len(audits) == 1
        assert audits[0].actor_id == "local_operator"
        assert audits[0].command_type == "TASK_ENQUEUE"
        assert len(audits[0].request_fingerprint) == 64


def test_same_idempotency_key_with_different_body_is_rejected(command_environment):
    client, Session = command_environment
    headers = {**AUTH, "Idempotency-Key": "command-enqueue-002"}
    first = client.post("/api/v1/commands/tasks/enqueue", json=_enqueue_body(), headers=headers)
    changed = client.post(
        "/api/v1/commands/tasks/enqueue",
        json=_enqueue_body(title="Different request"),
        headers=headers,
    )
    assert first.status_code == 200
    assert changed.status_code == 409
    with Session() as db:
        assert len(db.scalars(select(WorkTaskRecord)).all()) == 1
        assert len(db.scalars(select(WorkforceCommandAuditRecord)).all()) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"required_capability": "rule_validation", "task_type": "RULE_VALIDATION"},
        {"required_capability": "market_monitoring_simulation", "task_type": "MARKET_MONITORING_SIMULATION"},
        {"requested_worker_id": "decision_engineer"},
        {"requested_worker_id": "market_monitor"},
        {"task_type": "PLACE_ORDER"},
        {"actor_id": "admin"},
        {"non_trading_acknowledged": False},
    ],
)
def test_enqueue_rejects_trading_adjacent_or_untrusted_fields(command_environment, overrides):
    client, Session = command_environment
    response = client.post(
        "/api/v1/commands/tasks/enqueue",
        json=_enqueue_body(**overrides),
        headers={**AUTH, "Idempotency-Key": "command-reject-001"},
    )
    assert response.status_code == 422
    with Session() as db:
        assert db.scalar(select(WorkTaskRecord)) is None
        assert db.scalar(select(WorkforceCommandAuditRecord)) is None


def test_worker_pause_blocks_new_claims_without_disabling_worker(command_environment):
    client, Session = command_environment
    headers = {**AUTH, "Idempotency-Key": "command-pause-001"}
    paused = client.post(
        "/api/v1/commands/workers/scheduling",
        json={"worker_id": "haiku", "accepting_tasks": False},
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    with Session.begin() as db:
        worker = db.get(WorkerDefinitionRecord, "haiku")
        assert worker.enabled is True
        assert worker.accepting_tasks is False
        queued, created = enqueue_task(
            db,
            task_type="SUMMARIZATION",
            title="Queued while paused",
            required_capability="summarization",
            input_payload={"text": "safe"},
            idempotency_key="pause-queue-0001",
            requested_worker_id="haiku",
        )
        assert created is True
        assert claim_next_task(
            db, worker_id="haiku", instance_id="paused-instance-01",
        ) is None
    resumed = client.post(
        "/api/v1/commands/workers/scheduling",
        json={"worker_id": "haiku", "accepting_tasks": True},
        headers={**AUTH, "Idempotency-Key": "command-resume-001"},
    )
    assert resumed.status_code == 200, resumed.text
    with Session() as db:
        worker = db.get(WorkerDefinitionRecord, "haiku")
        assert worker.enabled is True
        assert worker.accepting_tasks is True
        events = db.scalars(select(WorkActivityEventRecord).where(
            WorkActivityEventRecord.worker_id == "haiku",
            WorkActivityEventRecord.event_type == "WORKER_STATE_CHANGED",
        )).all()
        assert len(events) == 2
        assert all(event.payload["field"] == "accepting_tasks" for event in events)


def test_operator_cannot_pause_trading_adjacent_workers(command_environment):
    client, Session = command_environment
    response = client.post(
        "/api/v1/commands/workers/scheduling",
        json={"worker_id": "decision_engineer", "accepting_tasks": False},
        headers={**AUTH, "Idempotency-Key": "command-pauserisk-01"},
    )
    assert response.status_code == 422
    with Session() as db:
        worker = db.get(WorkerDefinitionRecord, "decision_engineer")
        assert worker.accepting_tasks is True
        assert db.scalar(select(WorkforceCommandAuditRecord)) is None


def test_cancel_before_provider_request_is_cancelled(command_environment):
    client, Session = command_environment
    with Session.begin() as db:
        task, created = enqueue_task(
            db, task_type="SUMMARIZATION", title="Cancel before provider",
            required_capability="summarization", input_payload={"text": "safe"},
            idempotency_key="cancel-seed-0001", requested_worker_id="haiku",
        )
        assert created is True
        task_id = task.task_id
    response = client.post(
        "/api/v1/commands/tasks/cancel",
        json={"task_id": task_id},
        headers={**AUTH, "Idempotency-Key": "command-cancel-001"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"]["status"] == "CANCELLED"
    with Session() as db:
        task = db.get(WorkTaskRecord, task_id)
        assert task.status == "CANCELLED"
        audit = db.scalar(select(WorkforceCommandAuditRecord))
        assert audit.command_type == "TASK_CANCEL"


def test_cancel_after_provider_request_becomes_waiting_review(command_environment):
    client, Session = command_environment
    task_id = _seed_task(
        Session, task_id="task-provider-001", idempotency_key="provider-seed-001",
        status="RUNNING", lease_token="provider-lease-001",
        provider_request_id="provider-request-001", attempt_status="PROVIDER_REQUESTED",
    )
    response = client.post(
        "/api/v1/commands/tasks/cancel",
        json={"task_id": task_id},
        headers={**AUTH, "Idempotency-Key": "command-cancel-002"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"]["status"] == "WAITING_REVIEW"
    assert response.json()["outcome"]["provider_outcome_unknown"] is True
    with Session() as db:
        task = db.get(WorkTaskRecord, task_id)
        attempt = db.scalar(select(WorkTaskAttemptRecord).where(WorkTaskAttemptRecord.task_id == task_id))
        assert task.status == "WAITING_REVIEW"
        assert attempt.status == "UNKNOWN"
        assert attempt.error_code == "CANCEL_AFTER_PROVIDER_REQUEST"


def test_safe_retry_creates_new_task_and_keeps_original_failed(command_environment):
    client, Session = command_environment
    original_id = _seed_task(
        Session, task_id="task-failed-001", idempotency_key="failed-seed-001",
        status="FAILED", attempt_status="FAILED",
    )
    response = client.post(
        "/api/v1/commands/tasks/retry",
        json={"task_id": original_id},
        headers={**AUTH, "Idempotency-Key": "command-retry-001"},
    )
    assert response.status_code == 200, response.text
    result = response.json()["outcome"]
    assert result["retry_of_task_id"] == original_id
    assert result["task_id"] != original_id
    with Session() as db:
        original = db.get(WorkTaskRecord, original_id)
        retried = db.get(WorkTaskRecord, result["task_id"])
        assert original.status == "FAILED"
        assert retried.status == "QUEUED"
        assert retried.correlation_id == original.correlation_id
        assert retried.input_payload == original.input_payload


@pytest.mark.parametrize(
    "status,attempt_status,provider_request_id,purged",
    [
        ("WAITING_REVIEW", "UNKNOWN", "provider-unknown-001", False),
        ("FAILED", "PROVIDER_REQUESTED", "provider-request-002", False),
        ("FAILED", "FAILED", None, True),
    ],
)
def test_retry_rejects_uncertain_or_purged_task(
    command_environment, status, attempt_status, provider_request_id, purged,
):
    client, Session = command_environment
    purge_time = datetime.now(timezone.utc) if purged else None
    task_id = _seed_task(
        Session,
        task_id="task-reject-" + str(abs(hash((status, attempt_status, provider_request_id, purged))))[:12],
        idempotency_key="retry-reject-" + str(abs(hash((status, attempt_status, provider_request_id, purged))))[:12],
        status=status, attempt_status=attempt_status,
        provider_request_id=provider_request_id, payload_purged_at=purge_time,
    )
    response = client.post(
        "/api/v1/commands/tasks/retry",
        json={"task_id": task_id},
        headers={**AUTH, "Idempotency-Key": "command-retry-reject-01"},
    )
    assert response.status_code == 409
    with Session() as db:
        assert db.get(WorkTaskRecord, task_id).status == status


def test_rate_limit_is_enforced_before_second_command_side_effect():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=TABLES)
    Session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with Session.begin() as db:
        sync_default_registry(db)
    app = create_command_app(session_factory=Session, token=TOKEN, validate_database=False, rate_limit=1)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/commands/workers/scheduling",
            json={"worker_id": "haiku", "accepting_tasks": False},
            headers={**AUTH, "Idempotency-Key": "rate-limit-001"},
        )
        second = client.post(
            "/api/v1/commands/workers/scheduling",
            json={"worker_id": "haiku", "accepting_tasks": True},
            headers={**AUTH, "Idempotency-Key": "rate-limit-002"},
        )
    assert first.status_code == 200
    assert second.status_code == 429
    with Session() as db:
        worker = db.get(WorkerDefinitionRecord, "haiku")
        assert worker.accepting_tasks is False
        assert len(db.scalars(select(WorkforceCommandAuditRecord)).all()) == 1
    engine.dispose()


def test_command_api_rejects_oversized_chunked_and_declared_bodies_before_json_parsing(command_environment):
    client, Session = command_environment
    oversized = b"x" * (20 * 1024 + 1)

    # TestClient sets Content-Length. Middleware must reject before FastAPI's
    # JSON decoder or any command dependency can be reached.
    response = client.post(
        "/api/v1/commands/tasks/enqueue",
        content=oversized,
        headers={**AUTH, "Idempotency-Key": "command-body-limit-001", "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    with Session() as db:
        assert db.scalar(select(WorkTaskRecord)) is None
        assert db.scalar(select(WorkforceCommandAuditRecord)) is None


def test_retry_rejects_attempt_with_provider_start_timestamp_without_request_id(command_environment):
    client, Session = command_environment
    task_id = _seed_task(
        Session, task_id="task-reserved-001", idempotency_key="reserved-seed-001",
        status="FAILED", attempt_status="FAILED",
    )
    with Session.begin() as db:
        attempt = db.scalar(select(WorkTaskAttemptRecord).where(WorkTaskAttemptRecord.task_id == task_id))
        attempt.provider_started_at = datetime.now(timezone.utc)

    response = client.post(
        "/api/v1/commands/tasks/retry",
        json={"task_id": task_id},
        headers={**AUTH, "Idempotency-Key": "command-retry-provider-started"},
    )
    assert response.status_code == 409
    with Session() as db:
        assert len(db.scalars(select(WorkTaskRecord)).all()) == 1
        assert db.get(WorkTaskRecord, task_id).status == "FAILED"
