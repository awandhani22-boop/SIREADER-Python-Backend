"""Comprehensive test suite for Phase 5.2.2-B: Database Schema, Review Repository, Policy & Sanitization."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.ai.sanitizer import (
    MAX_REVIEW_PAYLOAD_BYTES,
    REDACTED,
    canonical_json_bytes,
    canonical_sha256,
    mask_string,
    sanitize_review_payload,
    validate_review_payload_size,
)
from app.governance.review_policy import (
    POLICY_VERSION,
    REVIEW_POLICY_CATALOG,
    TRADING_RELATED_CAPABILITIES,
    ReviewTriggerMode,
    evaluate_review_eligibility,
)
from app.workers.models import (
    ActorType,
    WorkActivityEventRecord,
    WorkTaskAttemptRecord,
    WorkTaskRecord,
    WorkforceCommandAuditRecord,
    WorkforceTaskReviewPayloadRecord,
    WorkforceTaskReviewRecord,
)
from app.workers.registry import CAPABILITY_CATALOG
from app.workers.repository import (
    claim_next_task,
    complete_task,
    enqueue_task,
    start_task,
    expire_stale_reviews,
    get_review_by_id,
    get_review_payload,
    heartbeat_worker,
    purge_expired_review_payloads,
    purge_expired_task_payloads,
    sync_default_registry,
)
from app.workers.runtime_contract import (
    ActivityEventType,
    AttemptStatus,
    ReviewStatus,
    TaskStatus,
    WorkerHeartbeatContract,
    WorkerProcessState,
)

DATABASE_URL = os.environ.get("SIREADER_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="SIREADER_TEST_POSTGRES_URL is required for live PostgreSQL tests",
)


@pytest.fixture(scope="module")
def db_engine():
    root = Path(__file__).resolve().parents[2]
    conn_url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(conn_url, autocommit=True) as admin:
        has_tasks = admin.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'workforce_tasks'").fetchone()
        if not has_tasks:
            for mig in (
                "2026_11_09_workforce_runtime_v1.sql",
                "2026_11_09_workforce_api_views_v1.sql",
                "2026_11_09_workforce_commands_v1.sql",
            ):
                mig_path = root / "migrations" / mig
                if mig_path.exists():
                    admin.execute(mig_path.read_text(encoding="utf-8"))

        has_related_task = admin.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'workforce_tasks' AND column_name = 'related_task_id'"
        ).fetchone()
        if not has_related_task:
            admin.execute((root / "migrations" / "2026_11_09_workforce_timeline_v2.sql").read_text(encoding="utf-8"))

        has_reviews = admin.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'workforce_task_reviews'").fetchone()
        has_cmd_audit = admin.execute("SELECT check_clause FROM information_schema.check_constraints WHERE constraint_name = 'workforce_command_audit_command_type_check'").fetchone()
        needs_migration = not has_reviews or (has_cmd_audit and "REVIEW_ADJUDICATE" not in has_cmd_audit[0])
        if needs_migration:
            admin.execute((root / "migrations" / "2026_11_10_workforce_reviews_v1.sql").read_text(encoding="utf-8"))

    engine = create_engine(DATABASE_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    SessionLocal = sessionmaker(bind=db_engine)
    session = SessionLocal()
    sync_default_registry(session)
    session.commit()
    yield session
    session.rollback()
    # Cancel active tasks
    session.execute(
        text("UPDATE public.workforce_tasks SET status = 'CANCELLED' WHERE status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'RETRY_WAIT')")
    )
    session.commit()
    session.close()


# -----------------------------------------------------------------------------
# Unit 1: Sanitizer & Canonical Hashing Tests
# -----------------------------------------------------------------------------

def test_sanitizer_recursive_redaction():
    payload = {
        "report_title": "Quarterly Analysis",
        "api_key": "sk-1234567890abcdef12345678",
        "nested": {
            "db_password": "supersecretpassword",
            "connection_string": "postgresql://user:pass@localhost:5432/db",
            "safe_items": ["item1", "item2"],
            "notes": "Access token is Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-ID1UR",
        },
        "details": [
            {"user_token": "token_value_abc"},
            {"public_info": "Google Cloud AIzaSyD-1234567890abcdef1234567890abc"},
        ],
    }

    sanitized = sanitize_review_payload(payload)

    assert sanitized["report_title"] == "Quarterly Analysis"
    assert sanitized["api_key"] == REDACTED
    assert sanitized["nested"]["db_password"] == REDACTED
    assert sanitized["nested"]["connection_string"] == REDACTED
    assert sanitized["nested"]["safe_items"] == ["item1", "item2"]
    assert REDACTED in sanitized["nested"]["notes"]
    assert "Bearer" not in sanitized["nested"]["notes"]
    assert sanitized["details"][0]["user_token"] == REDACTED
    assert REDACTED in sanitized["details"][1]["public_info"]


def test_canonical_json_and_sha256_determinism():
    payload1 = {"b": 2, "a": 1, "nested": {"z": 10, "m": 5}}
    payload2 = {"a": 1, "nested": {"m": 5, "z": 10}, "b": 2}

    bytes1 = canonical_json_bytes(payload1)
    bytes2 = canonical_json_bytes(payload2)
    assert bytes1 == bytes2 == b'{"a":1,"b":2,"nested":{"m":5,"z":10}}'

    hash1 = canonical_sha256(payload1)
    hash2 = canonical_sha256(payload2)
    assert hash1 == hash2
    assert len(hash1) == 64
    assert hash1.islower()


def test_payload_size_boundary_enforcement():
    small_payload = {"data": "x" * 1000}
    validate_review_payload_size(small_payload)  # Should not raise

    oversized_payload = {"data": "x" * (65 * 1024)}
    with pytest.raises(ValueError, match="exceeds maximum limit"):
        validate_review_payload_size(oversized_payload)


# -----------------------------------------------------------------------------
# Unit 2: Review Policy Catalog & Deterministic Sampling Tests
# -----------------------------------------------------------------------------

def test_review_policy_exhaustive_mapping_with_capability_catalog():
    """Verify that every capability and its task types are accounted for."""
    for capability_id, contract in CAPABILITY_CATALOG.items():
        if capability_id in TRADING_RELATED_CAPABILITIES:
            for tt in contract.task_types:
                assert evaluate_review_eligibility(capability_id, tt, "task_123") is False
        elif (capability_id, contract.task_types[0]) in REVIEW_POLICY_CATALOG:
            for tt in contract.task_types:
                rule = REVIEW_POLICY_CATALOG.get((capability_id, tt))
                assert rule is not None, f"Missing mapping for ({capability_id}, {tt})"
                assert rule.mode in (ReviewTriggerMode.MANDATORY, ReviewTriggerMode.SAMPLE, ReviewTriggerMode.NONE)


def test_review_policy_mandatory_and_sampling_rules():
    # Evaluation is MANDATORY
    assert evaluate_review_eligibility("evaluation", "EVALUATION", "task_any_1") is True
    assert evaluate_review_eligibility("evaluation", "REASONING", "task_any_2") is True

    # Complex reasoning (Opus) is MANDATORY
    assert evaluate_review_eligibility("complex_reasoning", "COMPLEX_REASONING", "task_any_3") is True

    # Data archiving is NONE
    assert evaluate_review_eligibility("data_archiving", "DATA_ARCHIVING", "task_any_4") is False

    # Sampling reproducibility
    task_id = "sample_task_998877"
    res1 = evaluate_review_eligibility("quick_research", "QUICK_RESEARCH", task_id)
    res2 = evaluate_review_eligibility("quick_research", "QUICK_RESEARCH", task_id)
    assert res1 == res2, "Deterministic sampling must produce identical outcome"


def test_trading_capabilities_strictly_barred():
    for trading_cap in TRADING_RELATED_CAPABILITIES:
        res = evaluate_review_eligibility(trading_cap, "DECISION_CHECK", "any_task")
        assert res is False


# -----------------------------------------------------------------------------
# Unit 3: Database Constraints & Triggers Tests
# -----------------------------------------------------------------------------

def test_db_payload_size_constraint(db_session: Session):
    review_id = f"rev_{uuid4().hex[:12]}"
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"

    # Setup parent task
    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="DB Constraint Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
    )
    db_session.add(task)
    db_session.flush()

    attempt = WorkTaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:12]}",
        status=AttemptStatus.COMPLETED.value,
    )
    db_session.add(attempt)
    db_session.flush()

    rev = WorkforceTaskReviewRecord(
        review_id=review_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=ReviewStatus.PENDING.value,
        original_output_sha256="a" * 64,
        review_expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    # Oversized payload (> 64 KiB) must violate check constraint
    oversized = {"big": "A" * (70 * 1024)}
    payload_rec = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output=oversized,
    )
    db_session.add(payload_rec)
    with pytest.raises(Exception) as exc_info:
        db_session.flush()
    assert "ck_workforce_review_payloads_size" in str(exc_info.value)
    db_session.rollback()


def test_db_original_payload_immutability(db_session: Session):
    review_id = f"rev_{uuid4().hex[:12]}"
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Immutability Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
    )
    db_session.add(task)
    db_session.flush()

    attempt = WorkTaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:12]}",
        status=AttemptStatus.COMPLETED.value,
    )
    db_session.add(attempt)
    db_session.flush()

    rev = WorkforceTaskReviewRecord(
        review_id=review_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=ReviewStatus.PENDING.value,
        original_output_sha256="b" * 64,
        review_expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    payload_rec = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output={"initial": "clean_result"},
    )
    db_session.add(payload_rec)
    db_session.flush()

    # Direct UPDATE on original_output must be rejected by trigger
    with pytest.raises(Exception) as exc_info:
        db_session.execute(
            text("UPDATE public.workforce_task_review_payloads SET original_output = '{\"tampered\": true}' WHERE review_id = :r"),
            {"r": review_id},
        )
    assert "Original review output is immutable" in str(exc_info.value)
    db_session.rollback()


def test_db_terminal_metadata_immutability(db_session: Session):
    review_id = f"rev_{uuid4().hex[:12]}"
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Terminal Immutability Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
    )
    db_session.add(task)
    db_session.flush()

    attempt = WorkTaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:12]}",
        status=AttemptStatus.COMPLETED.value,
    )
    db_session.add(attempt)
    db_session.flush()

    now = datetime.now(timezone.utc)
    rev = WorkforceTaskReviewRecord(
        review_id=review_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=ReviewStatus.APPROVED.value,
        original_output_sha256="c" * 64,
        adjudicated_by="operator_dan",
        adjudicated_at=now,
        review_expires_at=now + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    # Attempt to modify terminal review must be rejected by trigger
    with pytest.raises(Exception) as exc_info:
        db_session.execute(
            text("UPDATE public.workforce_task_reviews SET status = 'REJECTED' WHERE review_id = :r"),
            {"r": review_id},
        )
    assert "Terminal review state" in str(exc_info.value)
    db_session.rollback()


# -----------------------------------------------------------------------------
# Unit 4: Atomic Worker Completion & Output Quarantine Tests
# -----------------------------------------------------------------------------

def test_atomic_completion_quarantines_output_when_review_required(db_session: Session):
    now = datetime.now(timezone.utc)
    worker_id = "sonnet"
    instance_id = "inst_sonnet_01"
    heartbeat_worker(
        db_session,
        heartbeat=WorkerHeartbeatContract(
            worker_id=worker_id,
            instance_id=instance_id,
            process_state=WorkerProcessState.READY,
            process_started_at=now,
            heartbeat_at=now,
            observed_at=now,
            active_task_count=0,
            in_flight_provider_requests=0,
            schema_version=1,
        ),
    )
    db_session.commit()

    task, _ = enqueue_task(
        db_session,
        idempotency_key=f"idem_{uuid4().hex[:10]}",
        correlation_id=f"corr_{uuid4().hex[:10]}",
        task_type="EVALUATION",  # Mandatory review
        title="Atomic Review Quarantine Test",
        required_capability="evaluation",
        input_payload={"test": "data"},
        priority=60,
        now=now,
    )
    db_session.commit()

    claimed = claim_next_task(db_session, worker_id=worker_id, instance_id=instance_id, now=now + timedelta(seconds=1))
    assert claimed is not None
    claimed_task, attempt = claimed

    start_task(db_session, task_id=claimed_task.task_id, lease_token=attempt.lease_token, now=now)

    output_payload = {
        "evaluation_summary": "Passed all checklist items",
        "secret_token": "Bearer eyJsecret123",
        "db_url": "postgresql://usr:pwd@localhost/db",
    }

    # Complete task
    completed = complete_task(
        db_session,
        task_id=claimed_task.task_id,
        lease_token=attempt.lease_token,
        result_payload=output_payload,
        now=now,
    )
    db_session.commit()

    # Invariants verification:
    # 1. Task is COMPLETED technically
    assert completed.status == TaskStatus.COMPLETED.value
    # 2. Result is QUARANTINED (NULL)
    assert completed.result_payload is None

    # 3. Review record created with PENDING
    review = db_session.scalar(
        select(WorkforceTaskReviewRecord).where(WorkforceTaskReviewRecord.task_id == completed.task_id)
    )
    assert review is not None
    assert review.status == ReviewStatus.PENDING.value
    assert len(review.original_output_sha256) == 64

    # 4. Payload record created with sanitized output (Bearer and DB URL scrubbed)
    payload_rec = get_review_payload(db_session, review.review_id)
    assert payload_rec is not None
    assert payload_rec.original_output["secret_token"] == REDACTED
    assert payload_rec.original_output["db_url"] == REDACTED
    assert payload_rec.original_output["evaluation_summary"] == "Passed all checklist items"

    # 5. Canonical hash matches stored sanitized output
    assert review.original_output_sha256 == canonical_sha256(payload_rec.original_output)

    # 6. Event HUMAN_REVIEW_QUEUED recorded in append-only ledger
    event = db_session.scalar(
        select(WorkActivityEventRecord)
        .where(
            WorkActivityEventRecord.task_id == completed.task_id,
            WorkActivityEventRecord.event_type == ActivityEventType.HUMAN_REVIEW_QUEUED.value,
        )
    )
    assert event is not None
    assert event.payload["review_id"] == review.review_id
    assert event.payload["original_output_sha256"] == review.original_output_sha256


def test_atomic_completion_releases_output_directly_when_review_not_required(db_session: Session):
    now = datetime.now(timezone.utc)
    worker_id = "data_archivist"
    instance_id = "inst_archivist_01"
    heartbeat_worker(
        db_session,
        heartbeat=WorkerHeartbeatContract(
            worker_id=worker_id,
            instance_id=instance_id,
            process_state=WorkerProcessState.READY,
            process_started_at=now,
            heartbeat_at=now,
            observed_at=now,
            active_task_count=0,
            in_flight_provider_requests=0,
            schema_version=1,
        ),
    )
    db_session.commit()

    task, _ = enqueue_task(
        db_session,
        idempotency_key=f"idem_{uuid4().hex[:10]}",
        correlation_id=f"corr_{uuid4().hex[:10]}",
        task_type="DATA_ARCHIVING",  # Review mode: NONE
        title="Archival Direct Release Test",
        required_capability="data_archiving",
        input_payload={"archive_id": "arch_123"},
        priority=30,
        now=now,
    )
    db_session.commit()

    claimed = claim_next_task(db_session, worker_id=worker_id, instance_id=instance_id, now=now + timedelta(seconds=1))
    assert claimed is not None
    claimed_task, attempt = claimed

    start_task(db_session, task_id=claimed_task.task_id, lease_token=attempt.lease_token, now=now)

    output_payload = {"archive_status": "ARCHIVED", "chunks_stored": 4}

    completed = complete_task(
        db_session,
        task_id=claimed_task.task_id,
        lease_token=attempt.lease_token,
        result_payload=output_payload,
        now=now,
    )
    db_session.commit()

    # Invariants verification:
    assert completed.status == TaskStatus.COMPLETED.value
    assert completed.result_payload == output_payload  # Direct release!

    # Zero review record created
    review = db_session.scalar(
        select(WorkforceTaskReviewRecord).where(WorkforceTaskReviewRecord.task_id == completed.task_id)
    )
    assert review is None


# -----------------------------------------------------------------------------
# Unit 5: Retention & Expiration Lifecycle Tests
# -----------------------------------------------------------------------------

def test_retention_shields_pending_reviews(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_ret_shield_{uuid4().hex[:8]}"
    attempt_id = f"att_ret_shield_{uuid4().hex[:8]}"
    review_id = f"rev_ret_shield_{uuid4().hex[:8]}"

    # Task expired 1 hour ago
    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:8]}",
        correlation_id=f"corr_{uuid4().hex[:8]}",
        task_type="EVALUATION",
        title="Retention Shield Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now - timedelta(hours=1),
    )
    db_session.add(task)
    db_session.flush()

    attempt = WorkTaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:8]}",
        status=AttemptStatus.COMPLETED.value,
    )
    db_session.add(attempt)
    db_session.flush()

    rev = WorkforceTaskReviewRecord(
        review_id=review_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=ReviewStatus.PENDING.value,
        original_output_sha256="d" * 64,
        review_expires_at=now + timedelta(hours=12),
    )
    payload_rec = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output={"sensitive": "must_not_be_purged"},
    )
    db_session.add(rev)
    db_session.flush()
    db_session.add(payload_rec)
    db_session.commit()

    # Run purge on expired tasks
    purged_count = purge_expired_task_payloads(db_session, now=now)
    db_session.commit()

    # Verify task was SHIELDED and not purged
    reloaded_task = db_session.scalar(select(WorkTaskRecord).where(WorkTaskRecord.task_id == task_id))
    assert reloaded_task.payload_purged_at is None

    # Verify review payload was untouched
    reloaded_payload = get_review_payload(db_session, review_id)
    assert reloaded_payload.original_output == {"sensitive": "must_not_be_purged"}


def test_expiration_sweeper_transitions_stale_reviews(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_expire_{uuid4().hex[:8]}"
    attempt_id = f"att_expire_{uuid4().hex[:8]}"
    review_id = f"rev_expire_{uuid4().hex[:8]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:8]}",
        correlation_id=f"corr_{uuid4().hex[:8]}",
        task_type="EVALUATION",
        title="Expiration Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
    )
    db_session.add(task)
    db_session.flush()

    attempt = WorkTaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:8]}",
        status=AttemptStatus.COMPLETED.value,
    )
    db_session.add(attempt)
    db_session.flush()

    # Review expired 10 minutes ago
    rev = WorkforceTaskReviewRecord(
        review_id=review_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=ReviewStatus.PENDING.value,
        original_output_sha256="e" * 64,
        review_expires_at=now - timedelta(minutes=10),
    )
    db_session.add(rev)
    db_session.commit()

    # Run expiration sweeper
    expired_count = expire_stale_reviews(db_session, now=now)
    db_session.commit()

    assert expired_count >= 1
    reloaded_rev = get_review_by_id(db_session, review_id)
    assert reloaded_rev.status == ReviewStatus.EXPIRED.value
    assert reloaded_rev.adjudicated_by == "system"
    assert reloaded_rev.reason_code == "REVIEW_TIMED_OUT"

    # Verify HUMAN_REVIEW_EXPIRED event emitted
    event = db_session.scalar(
        select(WorkActivityEventRecord).where(
            WorkActivityEventRecord.task_id == task_id,
            WorkActivityEventRecord.event_type == ActivityEventType.HUMAN_REVIEW_EXPIRED.value,
        )
    )
    assert event is not None


def test_command_audit_constraint_accepts_review_adjudicate(db_session: Session):
    audit = WorkforceCommandAuditRecord(
        command_id=f"cmd_{uuid4().hex[:12]}",
        actor_id="operator_dan",
        command_type="REVIEW_ADJUDICATE",
        target_id=f"rev_{uuid4().hex[:12]}",
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        request_fingerprint="f" * 64,
        correlation_id=f"corr_{uuid4().hex[:12]}",
        outcome={"status": "APPROVED"},
    )
    db_session.add(audit)
    db_session.commit()

    reloaded = db_session.scalar(
        select(WorkforceCommandAuditRecord).where(WorkforceCommandAuditRecord.command_id == audit.command_id)
    )
    assert reloaded is not None
    assert reloaded.command_type == "REVIEW_ADJUDICATE"
