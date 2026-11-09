from pathlib import Path
"""Phase 5.2.1 lifecycle audit ledger, append-only triggers, and timeline tests."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.api.config import api_settings
from app.api.dependencies import get_ro_session
from app.api.server import create_app
from app.workers.models import (
    ActorType,
    WorkActivityEventRecord,
    WorkTaskRecord,
)
from app.workers.repository import (
    cancel_task,
    claim_next_task,
    enqueue_task,
    fail_task,
    heartbeat_worker,
    sync_default_registry,
    _append_event,
)
from app.workers.runtime_contract import (
    ActivityEventType,
    TaskStatus,
    WorkerHeartbeatContract,
    WorkerProcessState,
)

API_TOKEN = "phase5-timeline-test-token-0123456789"
RO_AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture()
def timeline_env(monkeypatch):
    import os
    import psycopg
    db_url = os.environ.get("SIREADER_TEST_POSTGRES_URL")
    if not db_url:
        pytest.skip("SIREADER_TEST_POSTGRES_URL not set; skipping live PostgreSQL tests")

    root = Path(__file__).resolve().parents[2]
    conn_url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(conn_url, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_views WHERE viewname = 'v_api_workforce_task_timeline'").fetchone()
        if not exists:
            for mig in (
                "2026_11_09_workforce_runtime_v1.sql",
                "2026_11_09_workforce_api_views_v1.sql",
                "2026_11_09_workforce_commands_v1.sql",
                "2026_11_09_workforce_timeline_v2.sql",
            ):
                admin.execute((root / "migrations" / mig).read_text(encoding="utf-8"))

    engine = create_engine(db_url, echo=False)
    Session = sessionmaker(bind=engine)

    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", API_TOKEN)

    app = create_app(manage_database_lifecycle=False)

    def _get_test_session():
        with Session() as s:
            yield s

    app.dependency_overrides[get_ro_session] = _get_test_session
    client = TestClient(app)

    with Session() as cleanup:
        cleanup.execute(text("UPDATE public.workforce_tasks SET status = 'CANCELLED' WHERE status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'RETRY_WAIT')"))
        cleanup.commit()

    yield {
        "engine": engine,
        "Session": Session,
        "client": client,
    }

    engine.dispose()


def test_workforce_activity_events_immutability(timeline_env):
    """Verify that UPDATE, DELETE, and TRUNCATE are blocked by triggers on workforce_activity_events."""
    Session = timeline_env["Session"]
    with Session() as db:
        evt = _append_event(
            db,
            event_type=ActivityEventType.TASK_ENQUEUED,
            correlation_id=uuid4().hex,
            task_id=uuid4().hex,
            payload={"test": True},
            actor_type=ActorType.OPERATOR,
            actor_id="local_operator",
        )
        db.commit()
        evt_id = evt.event_id

        # 1. Test UPDATE rejection
        with pytest.raises(Exception) as exc_update:
            db.execute(
                text("UPDATE public.workforce_activity_events SET actor_id = 'hacked' WHERE event_id = :id"),
                {"id": evt_id},
            )
            db.commit()
        assert "workforce activity events are append-only" in str(exc_update.value).lower()
        db.rollback()

        # 2. Test DELETE rejection
        with pytest.raises(Exception) as exc_delete:
            db.execute(
                text("DELETE FROM public.workforce_activity_events WHERE event_id = :id"),
                {"id": evt_id},
            )
            db.commit()
        assert "workforce activity events are append-only" in str(exc_delete.value).lower()
        db.rollback()

        # 3. Test TRUNCATE rejection
        with pytest.raises(Exception) as exc_trunc:
            db.execute(text("TRUNCATE TABLE public.workforce_activity_events"))
            db.commit()
        assert "workforce activity events are append-only" in str(exc_trunc.value).lower()
        db.rollback()


def test_workforce_timeline_deterministic_sequence(timeline_env):
    """Verify that concurrent events sharing identical timestamp order deterministically by event_seq."""
    Session = timeline_env["Session"]
    with Session() as db:
        fixed_time = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
        cid = uuid4().hex
        tid = uuid4().hex

        e1 = _append_event(db, event_type=ActivityEventType.TASK_ENQUEUED, correlation_id=cid, task_id=tid, occurred_at=fixed_time, actor_type=ActorType.OPERATOR, actor_id="operator1")
        e2 = _append_event(db, event_type=ActivityEventType.TASK_CLAIMED, correlation_id=cid, task_id=tid, occurred_at=fixed_time, actor_type=ActorType.WORKER, actor_id="haiku")
        e3 = _append_event(db, event_type=ActivityEventType.TASK_STARTED, correlation_id=cid, task_id=tid, occurred_at=fixed_time, actor_type=ActorType.WORKER, actor_id="haiku")
        db.commit()

        assert e1.event_seq < e2.event_seq < e3.event_seq


def test_workforce_task_timeline_api_lifecycle_and_retry(timeline_env):
    """Verify end-to-end task lifecycle, retry lineage, and timeline reconstruction via GET endpoint."""
    Session = timeline_env["Session"]
    client = timeline_env["client"]

    with Session() as db:
        sync_default_registry(db)
        now = datetime.now(timezone.utc)
        heartbeat_worker(db, WorkerHeartbeatContract(
            worker_id="haiku",
            instance_id="haiku-instance-timeline-test",
            process_started_at=now,
            heartbeat_at=now,
            observed_at=now,
            process_state=WorkerProcessState.READY,
            active_task_count=0,
            in_flight_provider_requests=0,
            schema_version=1,
        ))
        db.commit()

        cid = uuid4().hex

        # 1. Enqueue task A
        task_a, created = enqueue_task(
            db,
            task_type="QUICK_RESEARCH",
            title="Analyze Market Structure",
            required_capability="quick_research",
            input_payload={"symbol": "EURUSD"},
            idempotency_key=f"idem-test-{uuid4().hex}",
            correlation_id=cid,
            requested_worker_id="haiku",
            priority=100,
            actor_type=ActorType.OPERATOR,
            actor_id="local_operator",
        )
        db.commit()
        tid_a = task_a.task_id

        # 2. Claim task A
        claimed = claim_next_task(db, worker_id="haiku", instance_id="haiku-instance-timeline-test")
        assert claimed is not None
        task_rec, attempt_rec = claimed
        assert task_rec.task_id == tid_a
        db.commit()

        # 3. Fail task A deterministically
        fail_task(
            db,
            task_id=tid_a,
            lease_token=attempt_rec.lease_token,
            error_code="PARSE_FAILED",
            retryable=False,
        )
        db.commit()

        # Verify task A is FAILED
        task_a_reloaded = db.scalar(select(WorkTaskRecord).where(WorkTaskRecord.task_id == tid_a))
        assert task_a_reloaded.status == TaskStatus.FAILED.value

        # 4. Operator retries: Enqueue task B referencing task A
        task_b, created_b = enqueue_task(
            db,
            task_type=task_a_reloaded.task_type,
            title=f"Retry: {task_a_reloaded.title}",
            required_capability=task_a_reloaded.required_capability,
            input_payload=dict(task_a_reloaded.input_payload),
            idempotency_key=f"idem-retry-{uuid4().hex}",
            correlation_id=cid,
            requested_worker_id=task_a_reloaded.requested_worker_id,
            actor_type=ActorType.OPERATOR,
            actor_id="local_operator",
            related_task_id=tid_a,
        )
        # Emit TASK_RETRIED on task A linking task B
        _append_event(
            db,
            event_type=ActivityEventType.TASK_RETRIED,
            correlation_id=cid,
            task_id=tid_a,
            related_task_id=task_b.task_id,
            actor_type=ActorType.OPERATOR,
            actor_id="local_operator",
            payload={"retried_into_task_id": task_b.task_id, "retry_of_task_id": tid_a},
        )
        db.commit()
        tid_b = task_b.task_id

    # Test Timeline API on Task A
    resp_a = client.get(f"/api/v1/workforce/tasks/{tid_a}/timeline", headers=RO_AUTH)
    assert resp_a.status_code == 200, resp_a.text
    data_a = resp_a.json()
    assert data_a["task_id"] == tid_a
    assert data_a["task"]["status"] == "FAILED"
    assert data_a["retry_lineage"]["retried_by_task_id"] == tid_b
    assert data_a["retry_lineage"]["retry_of_task_id"] is None
    event_types_a = [e["event_type"] for e in data_a["events"]]
    assert "TASK_ENQUEUED" in event_types_a
    assert "TASK_CLAIMED" in event_types_a
    assert "TASK_FAILED" in event_types_a
    assert "TASK_RETRIED" in event_types_a

    # Verify event ordering is monotonically increasing
    seqs_a = [e["event_seq"] for e in data_a["events"]]
    assert seqs_a == sorted(seqs_a)

    # Test Timeline API on Task B
    resp_b = client.get(f"/api/v1/workforce/tasks/{tid_b}/timeline", headers=RO_AUTH)
    assert resp_b.status_code == 200, resp_b.text
    data_b = resp_b.json()
    assert data_b["task_id"] == tid_b
    assert data_b["task"]["status"] == "QUEUED"
    assert data_b["retry_lineage"]["retry_of_task_id"] == tid_a
    assert data_b["retry_lineage"]["retried_by_task_id"] is None

    # Test 404 on non-existent task
    resp_404 = client.get("/api/v1/workforce/tasks/non-existent-task-id-12345/timeline", headers=RO_AUTH)
    assert resp_404.status_code == 404

    # Test pagination
    resp_paginated = client.get(f"/api/v1/workforce/tasks/{tid_a}/timeline?limit=2&offset=0", headers=RO_AUTH)
    assert resp_paginated.status_code == 200
    data_p = resp_paginated.json()
    assert len(data_p["events"]) == 2
    assert data_p["limit"] == 2
    assert data_p["total_events"] >= 4


def test_workforce_timeline_payload_sanitization(timeline_env):
    """Verify that sensitive payloads, tokens, and raw keys are NOT exposed in timeline event summary."""
    Session = timeline_env["Session"]
    client = timeline_env["client"]

    with Session() as db:
        cid = uuid4().hex
        task, _ = enqueue_task(
            db,
            task_type="QUICK_RESEARCH",
            title="Sensitive Task",
            required_capability="quick_research",
            input_payload={"secret_api_key": "super-secret-123", "password": "confidential"},
            idempotency_key=f"idem-secret-{uuid4().hex}",
            correlation_id=cid,
            actor_type=ActorType.OPERATOR,
            actor_id="local_operator",
        )
        db.commit()
        tid = task.task_id

    resp = client.get(f"/api/v1/workforce/tasks/{tid}/timeline", headers=RO_AUTH)
    assert resp.status_code == 200
    data = resp.json()

    for event in data["events"]:
        summary = event["event_summary"]
        summary_str = str(summary).lower()
        assert "super-secret-123" not in summary_str
        assert "confidential" not in summary_str
        assert "secret_api_key" not in summary_str
        assert "password" not in summary_str


def test_timeline_single_direct_retry_enforcement(timeline_env):
    """Prove a failed task can only be directly retried once."""
    from app.workers.models import WorkTaskRecord, WorkTaskAttemptRecord
    from app.workers.repository import sync_default_registry
    from app.command_api.routes import retry_failed_non_trading_task, TaskIdRequest
    from fastapi import HTTPException
    import pytest

    Session = timeline_env["Session"]
    with Session() as db_session:
        sync_default_registry(db_session)
        db_session.commit()

        task_id = f"task_test_retry_single_{uuid4().hex[:8]}"

        task = WorkTaskRecord(
            task_id=task_id,
            idempotency_key=f"idem_single_{uuid4().hex}",
            correlation_id="corr_test_single_retry",
            schema_version=1,
            task_type="QUICK_RESEARCH",
            title="Single retry test",
            required_capability="quick_research",
            priority=100,
            status="FAILED",
            attempts=1,
            max_attempts=3,
            input_payload={"symbol": "EURUSD"},
        )
        db_session.add(task)
        db_session.flush()

        attempt = WorkTaskAttemptRecord(
            attempt_id=f"att_test_single_retry_{uuid4().hex[:8]}",
            task_id=task_id,
            attempt_number=1,
            worker_id="haiku",
            lease_token=f"lease_token_{uuid4().hex[:12]}",
            status="FAILED",
        )
        db_session.add(attempt)
        db_session.commit()

        # First retry succeeds
        req1 = TaskIdRequest(task_id=task_id)
        res1 = retry_failed_non_trading_task(req1, idempotency_key=f"idemp_retry_1_{uuid4().hex[:6]}", db=db_session)
        assert res1["command_type"] == "TASK_RETRY"

        # Second retry with different idempotency key fails with 409 TASK_ALREADY_RETRIED
        req2 = TaskIdRequest(task_id=task_id)
        with pytest.raises(HTTPException) as exc_info:
            retry_failed_non_trading_task(req2, idempotency_key=f"idemp_retry_2_{uuid4().hex[:6]}", db=db_session)
        assert exc_info.value.status_code == 409
        assert "TASK_ALREADY_RETRIED" in exc_info.value.detail
