"""Disposable PostgreSQL integration coverage for the generic workforce migration and repository."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.workers.repository import (
    cancel_task, claim_next_task, complete_task, enqueue_task, expire_deadline_tasks,
    heartbeat_worker, mark_provider_requested, recover_expired_leases,
    start_task, sync_default_registry, purge_expired_task_payloads,
)
from app.workers.runtime_contract import (
    ActivityEventType, AttemptStatus, TaskStatus, WorkerHeartbeatContract,
    WorkerProcessState,
)
from app.workers.models import WorkActivityEventRecord, WorkTaskAttemptRecord, WorkTaskRecord
from app.workers.repository import WorkerUnavailableError

POSTGRES_URL = os.getenv("SIREADER_TEST_POSTGRES_URL")
PSYCOPG_URL = (
    make_url(POSTGRES_URL).set(drivername="postgresql").render_as_string(hide_password=False)
    if POSTGRES_URL else None
)
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="Disposable PostgreSQL URL is not configured")


def _run_migration(admin, root: Path, name: str):
    admin.execute((root / "migrations" / name).read_text(), prepare=False)


def test_workforce_migration_security_and_task_lifecycle():
    psycopg = pytest.importorskip("psycopg")
    root = Path(__file__).resolve().parents[2]
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    migration_name = "2026_11_09_workforce_runtime_v1.sql"
    rollback_name = "2026_11_09_workforce_runtime_v1_rollback.sql"
    views_migration_name = "2026_11_09_workforce_api_views_v1.sql"
    command_migration_name = "2026_11_09_workforce_commands_v1.sql"
    command_rollback_name = "2026_11_09_workforce_commands_v1_rollback.sql"
    views_rollback_name = "2026_11_09_workforce_api_views_v1_rollback.sql"
    timeline_migration_name = "2026_11_09_workforce_timeline_v2.sql"
    timeline_rollback_name = "2026_11_09_workforce_timeline_v2_rollback.sql"
    review_migration_name = "2026_11_10_workforce_reviews_v1.sql"
    review_rollback_name = "2026_11_10_workforce_reviews_v1_rollback.sql"
    budget_migration_name = "2026_11_10_workforce_budget_observability_v1.sql"
    budget_rollback_name = "2026_11_10_workforce_budget_observability_v1_rollback.sql"

    with psycopg.connect(PSYCOPG_URL, autocommit=True) as admin:
        roles = {row[0] for row in admin.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('sireader_view_owner', 'sireader_api_ro', 'sireader_workforce_cmd')")}
        if roles != {"sireader_view_owner", "sireader_api_ro", "sireader_workforce_cmd"}:
            pytest.skip("Run after the disposable PostgreSQL privilege fixture creates the approved roles.")

        # This suite runs after another privilege suite in the same disposable
        # database. Reset only workforce objects so Phase 4 view migrations are
        # always tested from their actual pre-Phase-5 schema, not against a
        # previously upgraded view with an extra accepting_tasks column.
        for view_name in (
            "v_api_workforce_ai_usage", "v_api_workforce_task_reviews",
            "v_api_workforce_task_timeline", "v_api_workforce_runtime", "v_api_workforce_activity",
            "v_api_workforce_attempts", "v_api_workforce_tasks",
            "v_api_workforce_workers",
        ):
            admin.execute(f"DROP VIEW IF EXISTS public.{view_name} CASCADE")
        for table_name in (
            "workforce_task_review_payloads", "workforce_task_reviews",
            "workforce_command_audit", "workforce_activity_events",
            "workforce_task_attempts", "workforce_tasks", "workforce_heartbeats",
            "workforce_worker_capabilities", "workforce_capabilities", "workforce_workers",
        ):
            admin.execute(f"DROP TABLE IF EXISTS public.{table_name} CASCADE")
        admin.execute("DROP FUNCTION IF EXISTS public.sireader_notify_workforce_v1() CASCADE")
        admin.execute("DROP FUNCTION IF EXISTS public.sireader_reject_command_audit_mutation_v1() CASCADE")
        admin.execute("DROP FUNCTION IF EXISTS public.sireader_guard_review_payloads_v1() CASCADE")
        admin.execute("DROP FUNCTION IF EXISTS public.sireader_guard_review_metadata_v1() CASCADE")

        try:
            _run_migration(admin, root, migration_name)
            _run_migration(admin, root, migration_name)  # idempotency
            _run_migration(admin, root, views_migration_name)
            _run_migration(admin, root, views_migration_name)  # idempotent view replacement
            _run_migration(admin, root, command_migration_name)
            _run_migration(admin, root, command_migration_name)  # idempotent command boundary
            _run_migration(admin, root, timeline_migration_name)
            _run_migration(admin, root, timeline_migration_name)  # idempotent timeline boundary
            _run_migration(admin, root, review_migration_name)
            _run_migration(admin, root, review_migration_name)  # idempotent review boundary
            _run_migration(admin, root, budget_migration_name)
            _run_migration(admin, root, budget_migration_name)  # idempotent budget view
            # Verify rollback/forward behavior for both Phase 5 migrations.
            _run_migration(admin, root, budget_rollback_name)
            _run_migration(admin, root, review_rollback_name)
            _run_migration(admin, root, review_migration_name)
            _run_migration(admin, root, budget_migration_name)
            counts = admin.execute(
                """
                SELECT
                  (SELECT count(*) FROM public.workforce_workers),
                  (SELECT count(*) FROM public.workforce_capabilities),
                  (SELECT count(*) FROM public.workforce_worker_capabilities)
                """
            ).fetchone()
            assert counts == (7, 28, 29)

            # The browser-facing read-only role never gets direct workforce table access.
            with psycopg.connect(PSYCOPG_URL, autocommit=True) as api:
                api.execute("SET ROLE sireader_api_ro")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    api.execute("SELECT * FROM public.workforce_tasks")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    api.execute("INSERT INTO public.workforce_tasks (task_id,idempotency_key,correlation_id,task_type,title,required_capability) VALUES ('x','blocked-key','blocked-corr','SUMMARIZATION','Blocked','summarization')")
                assert api.execute("SELECT count(*) FROM public.v_api_workforce_workers").fetchone()[0] == 7
                haiku_capabilities = api.execute(
                    "SELECT capability_ids FROM public.v_api_workforce_workers WHERE worker_id = 'haiku'"
                ).fetchone()[0]
                assert set(haiku_capabilities) == {
                    "quick_research", "information_extraction", "summarization", "lightweight_classification"
                }
                runtime_view = api.execute("SELECT configured_workers, ready_workers FROM public.v_api_workforce_runtime").fetchone()
                assert runtime_view == (7, 0)
                assert api.execute("SELECT count(*) FROM public.v_api_workforce_tasks").fetchone()[0] == 0
                assert api.execute("SELECT count(*) FROM public.v_api_workforce_attempts").fetchone()[0] == 0
                assert api.execute("SELECT count(*) FROM public.v_api_workforce_activity").fetchone()[0] == 0
                assert api.execute("SELECT count(*) FROM public.v_api_workforce_task_reviews").fetchone()[0] == 0
                assert api.execute("SELECT count(*) FROM public.v_api_workforce_ai_usage").fetchone()[0] == 0

            now = datetime.now(timezone.utc).replace(microsecond=0)
            with Session() as db:
                with db.begin():
                    assert sync_default_registry(db) == 7
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-123",
                        process_state=WorkerProcessState.READY, process_started_at=now,
                        heartbeat_at=now, active_task_count=0, in_flight_provider_requests=0,
                        observed_at=now,
                    ))

            # A logical worker may run several process instances. The API view
            # must aggregate only fresh instance counters and keep READY if any
            # fresh instance is READY, rather than exposing just the latest row.
            multi_instance_at = now + timedelta(seconds=1)
            with Session() as db:
                with db.begin():
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-123",
                        process_state=WorkerProcessState.READY, process_started_at=now,
                        heartbeat_at=multi_instance_at, active_task_count=2,
                        in_flight_provider_requests=1, observed_at=multi_instance_at,
                    ))
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-secondary",
                        process_state=WorkerProcessState.READY, process_started_at=now,
                        heartbeat_at=multi_instance_at, active_task_count=3,
                        in_flight_provider_requests=2, observed_at=multi_instance_at,
                    ))
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-stale",
                        process_state=WorkerProcessState.READY, process_started_at=now - timedelta(minutes=2),
                        heartbeat_at=now - timedelta(minutes=2), active_task_count=9,
                        in_flight_provider_requests=9, observed_at=now - timedelta(minutes=2),
                    ))

            with psycopg.connect(PSYCOPG_URL, autocommit=True) as api:
                api.execute("SET ROLE sireader_api_ro")
                workforce_worker = api.execute(
                    "SELECT process_status, active_task_count, in_flight_provider_requests "
                    "FROM public.v_api_workforce_workers WHERE worker_id = 'haiku'"
                ).fetchone()
                assert workforce_worker == ("READY", 5, 3)

            # Clear the fixture's counters and leave both fresh instances with
            # their real process states before exercising task dispatch below.
            reset_at = now + timedelta(seconds=2)
            with Session() as db:
                with db.begin():
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-123",
                        process_state=WorkerProcessState.READY, process_started_at=now,
                        heartbeat_at=reset_at, active_task_count=0,
                        in_flight_provider_requests=0, observed_at=reset_at,
                    ))
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-secondary",
                        process_state=WorkerProcessState.STOPPED, process_started_at=now,
                        heartbeat_at=reset_at, active_task_count=0,
                        in_flight_provider_requests=0, observed_at=reset_at,
                    ))

            task_ids = []
            with Session() as db:
                with db.begin():
                    with pytest.raises(ValueError, match="JSON object"):
                        enqueue_task(
                            db, task_type="SUMMARIZATION", title="Invalid payload",
                            required_capability="summarization", input_payload=["not", "an object"],
                            idempotency_key="phase4-invalid-payload-0001", now=now,
                        )

            with Session() as db:
                with db.begin():
                    first, created = enqueue_task(
                        db, task_type="SUMMARIZATION", title="Summarize document",
                        required_capability="summarization", input_payload={"text": "safe test"},
                        idempotency_key="phase4-idempotency-001", priority=60, now=now,
                    )
                    assert created is True
                    duplicate, duplicate_created = enqueue_task(
                        db, task_type="SUMMARIZATION", title="Summarize document",
                        required_capability="summarization", input_payload={"text": "safe test"},
                        idempotency_key="phase4-idempotency-001", priority=60,
                    )
                    assert duplicate_created is False and duplicate.task_id == first.task_id
                    task_ids.append(first.task_id)
                    for idx in (2, 3):
                        row, created = enqueue_task(
                            db, task_type="SUMMARIZATION", title=f"Summarize document {idx}",
                            required_capability="summarization", input_payload={"index": idx},
                            idempotency_key=f"phase4-idempotency-{idx:03d}", priority=50, now=now,
                        )
                        assert created
                        task_ids.append(row.task_id)
                    deadline_row, deadline_created = enqueue_task(
                        db, task_type="SUMMARIZATION", title="Deadline task",
                        required_capability="summarization", input_payload={"index": 4},
                        idempotency_key="phase4-deadline-004", priority=40,
                        deadline_at=now + timedelta(seconds=30), not_before=now, now=now,
                    )
                    assert deadline_created
                    task_ids.append(deadline_row.task_id)

            def claim_one(_index):
                with Session() as db:
                    with db.begin():
                        claimed = claim_next_task(
                            db, worker_id="haiku", instance_id="instance-haiku-123",
                            lease_seconds=45, now=now,
                        )
                        return None if claimed is None else (claimed[0].task_id, claimed[1].attempt_id, claimed[1].lease_token)

            with ThreadPoolExecutor(max_workers=3) as pool:
                claimed = list(pool.map(claim_one, range(3)))
            active = [value for value in claimed if value is not None]
            assert len(active) == 2  # Haiku concurrency limit is two, even under parallel claims.
            assert len({item[0] for item in active}) == 2

            # After a provider request, cancellation is an uncertain outcome and cannot be retried.
            provider_task_id, provider_attempt_id, provider_lease = active[0]
            with Session() as db:
                with db.begin():
                    start_task(db, task_id=provider_task_id, lease_token=provider_lease, now=now + timedelta(seconds=1))
                    mark_provider_requested(db, task_id=provider_task_id, lease_token=provider_lease, now=now + timedelta(seconds=2))

            # Both leases expire; only the pre-provider task is safe to retry.
            pre_task_id, pre_attempt_id, pre_lease = active[1]
            recovery_time = now + timedelta(minutes=2)
            with Session() as db:
                with db.begin():
                    provider_task = db.get(WorkTaskRecord, provider_task_id)
                    pre_task = db.get(WorkTaskRecord, pre_task_id)
                    provider_task.lease_expires_at = now + timedelta(seconds=10)
                    pre_task.lease_expires_at = now + timedelta(seconds=10)
                    result = recover_expired_leases(db, now=recovery_time)
                    assert result == {"requeued": 1, "waiting_review": 1, "dead_lettered": 0}
                    assert provider_task.status == TaskStatus.WAITING_REVIEW.value
                    assert pre_task.status == TaskStatus.RETRY_WAIT.value
                    assert expire_deadline_tasks(db, now=recovery_time) == 1

            with Session() as db:
                with db.begin():
                    provider_attempt = db.get(WorkTaskAttemptRecord, provider_attempt_id)
                    assert provider_attempt.status == AttemptStatus.UNKNOWN.value

            # Heartbeat is refreshed; retry is eligible after the bounded backoff.
            with Session() as db:
                with db.begin():
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-123",
                        process_state=WorkerProcessState.READY, process_started_at=now,
                        heartbeat_at=recovery_time + timedelta(minutes=1),
                        active_task_count=0, in_flight_provider_requests=0,
                        observed_at=recovery_time + timedelta(minutes=1),
                    ))
                    retry = claim_next_task(
                        db, worker_id="haiku", instance_id="instance-haiku-123",
                        now=recovery_time + timedelta(minutes=1),
                    )
                    assert retry is not None and retry[0].task_id == pre_task_id
                    assert retry[0].attempts == 2
                    start_task(
                        db, task_id=retry[0].task_id, lease_token=retry[1].lease_token,
                        now=recovery_time + timedelta(minutes=1, seconds=1),
                    )
                    completed_at = recovery_time + timedelta(minutes=1, seconds=1)
                    complete_task(
                        db, task_id=retry[0].task_id, lease_token=retry[1].lease_token,
                        result_payload={"source": "SIMULATED", "summary": "test completed"},
                        now=completed_at,
                    )

            # A second claim without a matching fresh heartbeat is rejected; a real
            # queued task can be claimed and cancelled before provider dispatch.
            with Session() as db:
                with db.begin():
                    with pytest.raises(WorkerUnavailableError):
                        claim_next_task(
                            db, worker_id="haiku", instance_id="missing-instance",
                            now=recovery_time + timedelta(minutes=1, seconds=2),
                        )
                    heartbeat_worker(db, WorkerHeartbeatContract(
                        worker_id="haiku", instance_id="instance-haiku-123",
                        process_state=WorkerProcessState.READY, process_started_at=now,
                        heartbeat_at=recovery_time + timedelta(minutes=1, seconds=1),
                        active_task_count=0, in_flight_provider_requests=0,
                        observed_at=recovery_time + timedelta(minutes=1, seconds=1),
                    ))
                    pending = claim_next_task(
                        db, worker_id="haiku", instance_id="instance-haiku-123",
                        now=recovery_time + timedelta(minutes=1, seconds=2),
                    )
                    assert pending is not None
                    assert pending[0].task_id in {task_ids[1], task_ids[2]}
                    assert pending[0].task_id not in {pre_task_id, provider_task_id}
                    cancelled = cancel_task(db, task_id=pending[0].task_id, now=recovery_time + timedelta(minutes=1, seconds=3))
                    assert cancelled.status == TaskStatus.CANCELLED.value

            # Terminal payloads expire after 30 days; unresolved provider outcomes
            # keep their payload for the longer 90-day review window. Metadata stays.
            with Session() as db:
                with db.begin():
                    completed = db.get(WorkTaskRecord, pre_task_id)
                    review = db.get(WorkTaskRecord, provider_task_id)
                    assert completed.payload_expires_at == completed_at + timedelta(days=30)
                    assert review.payload_expires_at == recovery_time + timedelta(days=90)
                    assert purge_expired_task_payloads(db, now=recovery_time + timedelta(days=31)) == 3

            with Session() as db:
                completed = db.get(WorkTaskRecord, pre_task_id)
                review = db.get(WorkTaskRecord, provider_task_id)
                deadline = db.get(WorkTaskRecord, task_ids[3])
                cancelled = db.get(WorkTaskRecord, pending[0].task_id)
                assert completed.input_payload == {}
                assert completed.result_payload is None
                assert completed.payload_purged_at is not None
                assert deadline.payload_purged_at is not None
                assert cancelled.payload_purged_at is not None
                assert review.input_payload == {"text": "safe test"}
                assert review.payload_purged_at is None

            with Session() as db:
                with db.begin():
                    assert purge_expired_task_payloads(db, now=recovery_time + timedelta(days=91)) == 1
            with Session() as db:
                review = db.get(WorkTaskRecord, provider_task_id)
                assert review.input_payload == {}
                assert review.payload_purged_at is not None

            # A purged task cannot be requeued without its original input.
            with pytest.raises(IntegrityError):
                with Session() as db:
                    with db.begin():
                        review = db.get(WorkTaskRecord, provider_task_id)
                        review.status = TaskStatus.QUEUED.value
                        db.flush()
            with Session() as db:
                review = db.get(WorkTaskRecord, provider_task_id)
                assert review.status == TaskStatus.WAITING_REVIEW.value
                assert review.payload_purged_at is not None

            with Session() as db:
                events = db.query(WorkActivityEventRecord).filter(WorkActivityEventRecord.task_id.in_(task_ids)).all()
                assert any(item.event_type == ActivityEventType.TASK_WAITING_REVIEW.value for item in events)
                assert any(item.event_type == ActivityEventType.TASK_COMPLETED.value for item in events)
                assert db.query(WorkTaskAttemptRecord).filter(WorkTaskAttemptRecord.task_id == pre_task_id).count() == 2

            # Fixed-resource notification contains no row data.
            with psycopg.connect(PSYCOPG_URL, autocommit=True) as listener:
                listener.execute("LISTEN sireader_observability_v1")
                with psycopg.connect(PSYCOPG_URL) as writer:
                    writer.execute("UPDATE public.workforce_workers SET updated_at = now() WHERE worker_id = 'haiku'")
                    writer.commit()
                notices = list(listener.notifies(timeout=2.0, stop_after=1))
                assert [item.payload for item in notices] == ["workforce"]

        finally:
            _run_migration(admin, root, budget_rollback_name)
            _run_migration(admin, root, review_rollback_name)
            _run_migration(admin, root, timeline_rollback_name)
            _run_migration(admin, root, command_rollback_name)
            _run_migration(admin, root, views_rollback_name)
            _run_migration(admin, root, rollback_name)
    engine.dispose()
