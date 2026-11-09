"""Comprehensive test suite for Phase 5.2.2-B: Database Schema, Review Repository, Policy & Sanitization."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
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
from app.workers.policy import (
    POLICY_VERSION as WORKFORCE_POLICY_VERSION,
    SensitiveTaskAction, evaluate_workforce_policy,
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
from app.workers.registry import CAPABILITY_CATALOG, build_default_registry
from app.workers.repository import (
    WorkforceRepositoryError,
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

        # Always apply the review migration to ensure latest triggers and constraints are in place
        admin.execute((root / "migrations" / "2026_11_10_workforce_reviews_v1.sql").read_text(encoding="utf-8"))
        admin.execute((root / "migrations" / "2026_11_10_workforce_review_payload_insert_guard_v2.sql").read_text(encoding="utf-8"))

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
    """Verify that every capability and its task types are accounted for without skipping."""
    for capability_id, contract in CAPABILITY_CATALOG.items():
        if capability_id in TRADING_RELATED_CAPABILITIES:
            for tt in contract.task_types:
                assert evaluate_review_eligibility(capability_id, tt, "task_123") is False
        else:
            for tt in contract.task_types:
                rule = REVIEW_POLICY_CATALOG.get((capability_id, tt))
                assert rule is not None, f"Missing policy mapping for non-trading pair ({capability_id}, {tt})"
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


# -----------------------------------------------------------------------------
# Unit 4: Hardening Integrity Tests (ChatGPT 5.2.2-B Directive)
# -----------------------------------------------------------------------------

def test_db_retention_purge_guard_before_task_expiry_rejected(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"
    review_id = f"rev_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Premature Purge Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now + timedelta(days=30),
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
        review_expires_at=now + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    payload = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output={"report": "classified"},
    )
    db_session.add(payload)
    db_session.commit()
    db_session.execute(
        text(
            "UPDATE public.workforce_task_reviews "
            "SET status = 'APPROVED', adjudicated_by = 'operator_dan', adjudicated_at = now() "
            "WHERE review_id = :rid"
        ),
        {"rid": review_id},
    )
    db_session.commit()

    with pytest.raises(Exception, match="before parent task expiration"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET original_output = NULL, adjusted_output = NULL, payload_purged_at = now() "
                "WHERE review_id = :rid"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()


def test_db_retention_purge_guard_pending_review_rejected(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"
    review_id = f"rev_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Pending Purge Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now - timedelta(days=1),
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
        review_expires_at=now + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    payload = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output={"report": "under_review"},
    )
    db_session.add(payload)
    db_session.commit()

    with pytest.raises(Exception, match="Cannot purge payloads for active review"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET original_output = NULL, adjusted_output = NULL, payload_purged_at = now() "
                "WHERE review_id = :rid"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()


def test_db_retention_purge_and_anti_resurrection(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"
    review_id = f"rev_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Purge Success Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now - timedelta(days=2),
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
        original_output_sha256="c" * 64,
        review_expires_at=now + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    payload = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output={"data": "to_be_purged"},
    )
    db_session.add(payload)
    db_session.commit()
    db_session.execute(
        text(
            "UPDATE public.workforce_task_reviews "
            "SET status = 'APPROVED', adjudicated_by = 'operator_dan', adjudicated_at = now() "
            "WHERE review_id = :rid"
        ),
        {"rid": review_id},
    )
    db_session.commit()

    # 1. Purge succeeds for terminal review after task expiry
    db_session.execute(
        text(
            "UPDATE public.workforce_task_review_payloads "
            "SET original_output = NULL, adjusted_output = NULL, payload_purged_at = now() "
            "WHERE review_id = :rid"
        ),
        {"rid": review_id},
    )
    db_session.commit()

    purged_rec = db_session.get(WorkforceTaskReviewPayloadRecord, review_id)
    assert purged_rec.original_output is None
    assert purged_rec.payload_purged_at is not None

    # 2. Second purge is rejected
    with pytest.raises(Exception, match="Purged review payload cannot be modified or restored"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET payload_purged_at = now() "
                "WHERE review_id = :rid"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()

    # 3. Restoring payload after purge is rejected
    with pytest.raises(Exception, match="Purged review payload cannot be modified or restored"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET original_output = '{\"resurrected\": true}'::jsonb "
                "WHERE review_id = :rid"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()




def test_db_initial_review_payload_insert_rejects_adjusted_output(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"
    review_id = f"rev_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Initial payload insert guard",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now + timedelta(days=30),
    )
    db_session.add(task)
    db_session.flush()
    db_session.add(WorkTaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:12]}",
        status=AttemptStatus.COMPLETED.value,
    ))
    db_session.flush()
    db_session.add(WorkforceTaskReviewRecord(
        review_id=review_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=ReviewStatus.PENDING.value,
        original_output_sha256="e" * 64,
        review_expires_at=now + timedelta(hours=24),
    ))
    db_session.commit()

    # A command DB login can INSERT payload rows, but only the original quarantined
    # result may exist at insertion time. It cannot pre-seed an edited result.
    with pytest.raises(Exception, match="Initial review payload may contain only original_output"):
        db_session.execute(
            text(
                "INSERT INTO public.workforce_task_review_payloads "
                "(review_id, original_output, adjusted_output) "
                "VALUES (:rid, '{\"original\": true}'::jsonb, '{\"forged_edit\": true}'::jsonb)"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()

    db_session.execute(
        text(
            "INSERT INTO public.workforce_task_review_payloads "
            "(review_id, original_output, adjusted_output) "
            "VALUES (:rid, '{\"original\": true}'::jsonb, NULL)"
        ),
        {"rid": review_id},
    )
    db_session.commit()
    payload = db_session.get(WorkforceTaskReviewPayloadRecord, review_id)
    assert payload is not None
    assert payload.original_output == {"original": True}
    assert payload.adjusted_output is None


def test_db_adjusted_output_state_guard(db_session: Session):
    now = datetime.now(timezone.utc)
    task_id = f"task_{uuid4().hex[:12]}"
    attempt_id = f"att_{uuid4().hex[:12]}"
    review_id = f"rev_{uuid4().hex[:12]}"

    task = WorkTaskRecord(
        task_id=task_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Adjusted Guard Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now + timedelta(days=30),
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
        original_output_sha256="d" * 64,
        review_expires_at=now + timedelta(hours=24),
    )
    db_session.add(rev)
    db_session.flush()

    payload = WorkforceTaskReviewPayloadRecord(
        review_id=review_id,
        original_output={"report": "draft"},
    )
    db_session.add(payload)
    db_session.commit()

    # 1. Writing adjusted_output while review is PENDING must be rejected
    with pytest.raises(Exception, match="Adjusted review output can only be written for reviews in EDITED status"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET adjusted_output = '{\"edit\": 1}'::jsonb "
                "WHERE review_id = :rid"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()

    # 2. Writing adjusted_output when review is APPROVED must be rejected
    db_session.execute(
        text(
            "UPDATE public.workforce_task_reviews "
            "SET status = 'APPROVED', adjudicated_by = 'op', adjudicated_at = now() "
            "WHERE review_id = :rid"
        ),
        {"rid": review_id},
    )
    db_session.commit()

    with pytest.raises(Exception, match="Adjusted review output can only be written for reviews in EDITED status"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET adjusted_output = '{\"edit\": 1}'::jsonb "
                "WHERE review_id = :rid"
            ),
            {"rid": review_id},
        )
        db_session.commit()
    db_session.rollback()

    # 3. Create a fresh review to test EDITED transition
    rev_edit_id = f"rev_{uuid4().hex[:12]}"
    task_edit_id = f"task_{uuid4().hex[:12]}"
    att_edit_id = f"att_{uuid4().hex[:12]}"

    task_edit = WorkTaskRecord(
        task_id=task_edit_id,
        idempotency_key=f"idem_{uuid4().hex[:12]}",
        correlation_id=f"corr_{uuid4().hex[:12]}",
        task_type="EVALUATION",
        title="Edit Success Test",
        required_capability="evaluation",
        status=TaskStatus.COMPLETED.value,
        payload_expires_at=now + timedelta(days=30),
    )
    db_session.add(task_edit)
    db_session.flush()

    att_edit = WorkTaskAttemptRecord(
        attempt_id=att_edit_id,
        task_id=task_edit_id,
        attempt_number=1,
        worker_id="sonnet",
        lease_token=f"lt_{uuid4().hex[:12]}",
        status=AttemptStatus.COMPLETED.value,
    )
    db_session.add(att_edit)
    db_session.flush()

    rev_edit = WorkforceTaskReviewRecord(
        review_id=rev_edit_id,
        task_id=task_edit_id,
        attempt_id=att_edit_id,
        status=ReviewStatus.PENDING.value,
        original_output_sha256="1" * 64,
        review_expires_at=now + timedelta(hours=24),
    )
    db_session.add(rev_edit)
    db_session.flush()

    payload_edit = WorkforceTaskReviewPayloadRecord(
        review_id=rev_edit_id,
        original_output={"original": "text"},
    )
    db_session.add(payload_edit)
    db_session.commit()

    # Update review to EDITED first
    db_session.execute(
        text(
            "UPDATE public.workforce_task_reviews "
            "SET status = 'EDITED', adjusted_output_sha256 = :adj_sha, adjudicated_by = 'op', adjudicated_at = now() "
            "WHERE review_id = :rid"
        ),
        {"rid": rev_edit_id, "adj_sha": "2" * 64},
    )
    # Then write adjusted_output -> MUST SUCCEED
    db_session.execute(
        text(
            "UPDATE public.workforce_task_review_payloads "
            "SET adjusted_output = '{\"edited\": true}'::jsonb "
            "WHERE review_id = :rid"
        ),
        {"rid": rev_edit_id},
    )
    db_session.commit()

    # 4. Modifying adjusted_output a second time must fail (write-once)
    with pytest.raises(Exception, match="Adjusted review output is write-once"):
        db_session.execute(
            text(
                "UPDATE public.workforce_task_review_payloads "
                "SET adjusted_output = '{\"modified_again\": true}'::jsonb "
                "WHERE review_id = :rid"
            ),
            {"rid": rev_edit_id},
        )
        db_session.commit()
    db_session.rollback()


def test_fail_closed_purge_inspection(db_session: Session, monkeypatch):
    import sqlalchemy
    def broken_inspect(bind):
        raise RuntimeError("Simulated connection timeout during inspection")

    monkeypatch.setattr(sqlalchemy, "inspect", broken_inspect)
    with pytest.raises(WorkforceRepositoryError, match="review subsystem inspection failed"):
        purge_expired_task_payloads(db_session)


def test_migration_forward_rollback_forward_lifecycle():
    root = Path(__file__).resolve().parents[2]
    conn_url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    mig = (root / "migrations" / "2026_11_10_workforce_reviews_v1.sql").read_text(encoding="utf-8")
    rb = (root / "migrations" / "2026_11_10_workforce_reviews_v1_rollback.sql").read_text(encoding="utf-8")
    mig_v2 = (root / "migrations" / "2026_11_10_workforce_review_payload_insert_guard_v2.sql").read_text(encoding="utf-8")
    rb_v2 = (root / "migrations" / "2026_11_10_workforce_review_payload_insert_guard_v2_rollback.sql").read_text(encoding="utf-8")

    with psycopg.connect(conn_url, autocommit=True) as conn:
        conn.execute(rb_v2)
        conn.execute(rb)
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'workforce_task_reviews'"
        ).fetchone()
        assert exists is None

        conn.execute(mig)
        conn.execute(mig_v2)
        trigger = conn.execute(
            "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
            "WHERE tgname = 'trg_workforce_task_review_payloads_guard' AND NOT tgisinternal"
        ).fetchone()
        assert trigger is not None and "INSERT" in trigger[0].upper()
        conn.execute(rb_v2)
        reverted = conn.execute(
            "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
            "WHERE tgname = 'trg_workforce_task_review_payloads_guard' AND NOT tgisinternal"
        ).fetchone()
        assert reverted is not None and "INSERT" not in reverted[0].upper()
        conn.execute(mig_v2)
        recreated = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'workforce_task_reviews'"
        ).fetchone()
        assert recreated is not None


def test_workforce_policy_routes_model_tier_from_capability_and_worker():
    registry = build_default_registry()
    cases = [
        ("haiku", "quick_research", "QUICK_RESEARCH", "FAST"),
        ("sonnet", "data_analysis", "DATA_ANALYSIS", "REASONING"),
        ("opus", "strategic_analysis", "STRATEGIC_ANALYSIS", "ESCALATION"),
    ]
    for worker_id, capability, task_type, expected_tier in cases:
        result = evaluate_workforce_policy(
            worker=registry[worker_id], task_type=task_type,
            required_capability=capability, title="Safe task",
            input_payload={"record": "plain"}, provider_mode="OMNIROUTE",
        )
        assert result.action is SensitiveTaskAction.ALLOW
        assert result.model_tier == expected_tier
        assert result.policy_version == WORKFORCE_POLICY_VERSION
        assert result.requires_human_review is False


def test_workforce_policy_redacts_credentials_before_any_provider_request():
    registry = build_default_registry()
    result = evaluate_workforce_policy(
        worker=registry["haiku"], task_type="QUICK_RESEARCH",
        required_capability="quick_research", title="Research bearer token",
        input_payload={
            "api_token": "private-value",
            "description": "Use Bearer abc.def.ghi only as sample",
        },
        provider_mode="OMNIROUTE",
    )
    assert result.action is SensitiveTaskAction.REDACT
    assert result.sanitized_input["api_token"] == REDACTED
    assert "private-value" not in str(result.sanitized_input)
    assert "Bearer abc.def.ghi" not in str(result.sanitized_input)
    assert result.requires_human_review is False


def test_workforce_policy_personal_data_is_redacted_and_forces_output_review():
    registry = build_default_registry()
    result = evaluate_workforce_policy(
        worker=registry["sonnet"], task_type="DATA_ANALYSIS",
        required_capability="data_analysis", title="Analyze contact list",
        input_payload={
            "customer": {"email": "person@example.com", "status": "active"},
        },
        provider_mode="OMNIROUTE",
    )
    assert result.action is SensitiveTaskAction.REVIEW
    assert result.sanitized_input["customer"]["email"] == "[REDACTED_PERSONAL_DATA]"
    assert result.requires_human_review is True
    assert result.redacted_fields == 1


def test_workforce_policy_rejects_live_execution_private_keys_and_market_workers():
    registry = build_default_registry()
    trade = evaluate_workforce_policy(
        worker=registry["haiku"], task_type="QUICK_RESEARCH",
        required_capability="quick_research", title="Execute a live trade",
        input_payload={"request": "ordinary research"}, provider_mode="OMNIROUTE",
    )
    private_key = evaluate_workforce_policy(
        worker=registry["haiku"], task_type="QUICK_RESEARCH",
        required_capability="quick_research", title="Inspect this key",
        input_payload={"data": "-----BEGIN PRIVATE KEY-----\nsecret"},
        provider_mode="OMNIROUTE",
    )
    market = evaluate_workforce_policy(
        worker=registry["market_monitor"], task_type="MARKET_WATCH",
        required_capability="market_monitoring_simulation",
        title="Summarize simulated market feed", input_payload={"ticks": []},
        provider_mode="OMNIROUTE",
    )
    assert trade.action is SensitiveTaskAction.REJECT
    assert private_key.action is SensitiveTaskAction.REJECT
    assert market.action is SensitiveTaskAction.REJECT
    assert market.reason_code == "LIVE_PROVIDER_WORKER_FORBIDDEN"


def test_workforce_policy_fails_closed_on_capability_task_mismatch():
    registry = build_default_registry()
    result = evaluate_workforce_policy(
        worker=registry["haiku"], task_type="STRATEGIC_ANALYSIS",
        required_capability="quick_research", title="Mismatched task",
        input_payload={}, provider_mode="OMNIROUTE",
    )
    assert result.action is SensitiveTaskAction.REJECT
    assert result.reason_code == "CAPABILITY_TASK_MISMATCH"


def test_workforce_policy_mock_mode_never_routes_provider_tier_to_excluded_workers():
    registry = build_default_registry()
    result = evaluate_workforce_policy(
        worker=registry["market_monitor"], task_type="MARKET_WATCH",
        required_capability="market_monitoring_simulation",
        title="Simulated feed", input_payload={"ticks": []}, provider_mode="MOCK",
    )
    assert result.action is SensitiveTaskAction.ALLOW
    assert result.model_tier == "NONE"


def test_sensitive_task_policy_forces_atomic_human_output_review(db_session: Session):
    now = datetime.now(timezone.utc)
    worker_id = "data_archivist"
    heartbeat_worker(
        db_session,
        heartbeat=WorkerHeartbeatContract(
            worker_id=worker_id,
            instance_id="inst_sensitive_review_01",
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
        task_type="DATA_ARCHIVING",
        title="Sensitive reviewed archive",
        required_capability="data_archiving",
        input_payload={"archive_id": "arch_sensitive"},
        priority=40,
        now=now,
    )
    db_session.commit()
    claimed = claim_next_task(
        db_session, worker_id=worker_id, instance_id="inst_sensitive_review_01",
        now=now + timedelta(seconds=1),
    )
    assert claimed is not None
    claimed_task, attempt = claimed
    start_task(db_session, task_id=claimed_task.task_id, lease_token=attempt.lease_token, now=now)
    completed = complete_task(
        db_session,
        task_id=claimed_task.task_id,
        lease_token=attempt.lease_token,
        result_payload={"summary": "Redacted personal-data review result"},
        force_human_review=True,
        review_trigger_reason="SENSITIVE_TASK_REVIEW",
        policy_context={
            "policy_version": POLICY_VERSION,
            "sensitive_action": "REVIEW",
            "model_tier": "NONE",
            "reason_code": "SENSITIVE_PERSONAL_DATA_REVIEW",
            "redacted_fields": 1,
        },
        now=now,
    )
    db_session.commit()
    assert completed.status == TaskStatus.COMPLETED.value
    assert completed.result_payload is None
    review = db_session.scalar(
        select(WorkforceTaskReviewRecord).where(
            WorkforceTaskReviewRecord.task_id == completed.task_id,
        )
    )
    assert review is not None and review.status == ReviewStatus.PENDING.value
    queued = db_session.scalar(
        select(WorkActivityEventRecord).where(
            WorkActivityEventRecord.task_id == completed.task_id,
            WorkActivityEventRecord.event_type == ActivityEventType.HUMAN_REVIEW_QUEUED.value,
        )
    )
    assert queued is not None
    assert queued.payload["trigger_reason"] == "SENSITIVE_TASK_REVIEW"
    complete_event = db_session.scalar(
        select(WorkActivityEventRecord).where(
            WorkActivityEventRecord.task_id == completed.task_id,
            WorkActivityEventRecord.event_type == ActivityEventType.TASK_COMPLETED.value,
        )
    )
    assert complete_event is not None
    assert complete_event.payload["sensitive_action"] == "REVIEW"
    assert complete_event.payload["policy_version"] == POLICY_VERSION


def test_run_maintenance_once_orders_expiry_before_payload_purge(monkeypatch):
    from app.workers import maintenance

    calls = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @contextmanager
        def begin(self):
            yield self

    monkeypatch.setattr(maintenance, "recover_expired_leases",
                        lambda db, now=None: calls.append("recover") or {"requeued": 1, "waiting_review": 2, "dead_lettered": 3})
    monkeypatch.setattr(maintenance, "expire_deadline_tasks",
                        lambda db, now=None: calls.append("deadlines") or 4)
    monkeypatch.setattr(maintenance, "expire_stale_reviews",
                        lambda db, now=None: calls.append("expire_reviews") or 5)
    monkeypatch.setattr(maintenance, "purge_expired_task_payloads",
                        lambda db, now=None: calls.append("purge_tasks") or 6)
    monkeypatch.setattr(maintenance, "purge_expired_review_payloads",
                        lambda db, now=None: calls.append("purge_reviews") or 7)

    result = maintenance.run_maintenance_once(
        session_factory=FakeSession,
        now=datetime(2026, 11, 10, tzinfo=timezone.utc),
    )

    assert calls == ["recover", "deadlines", "expire_reviews", "purge_tasks", "purge_reviews"]
    assert result == {
        "leases_recovered_requeued": 1,
        "leases_recovered_waiting_review": 2,
        "leases_recovered_dead_lettered": 3,
        "deadline_tasks_expired": 4,
        "stale_reviews_expired": 5,
        "task_payloads_purged": 6,
        "review_payloads_purged": 7,
    }


def test_workforce_policy_covers_every_general_provider_capability_and_task_pair():
    from app.governance.review_policy import TRADING_RELATED_CAPABILITIES
    from app.workers.policy import CAPABILITY_MODEL_TIERS, WORKER_TIER_CEILINGS

    registry = build_default_registry()
    for worker_id in ("haiku", "sonnet", "opus"):
        worker = registry[worker_id]
        for capability_id in worker.capability_ids:
            contract = CAPABILITY_CATALOG[capability_id]
            if capability_id in TRADING_RELATED_CAPABILITIES:
                for task_type in contract.task_types:
                    result = evaluate_workforce_policy(
                        worker=worker, task_type=task_type,
                        required_capability=capability_id, title="Policy matrix",
                        input_payload={}, provider_mode="OMNIROUTE",
                    )
                    assert result.action is SensitiveTaskAction.REJECT
                    assert result.reason_code == "TRADING_CAPABILITY_PROVIDER_FORBIDDEN"
                continue

            assert capability_id in CAPABILITY_MODEL_TIERS, (
                f"Provider capability {worker_id}:{capability_id} lacks a model tier"
            )
            assert CAPABILITY_MODEL_TIERS[capability_id] == WORKER_TIER_CEILINGS[worker_id]
            for task_type in contract.task_types:
                result = evaluate_workforce_policy(
                    worker=worker, task_type=task_type,
                    required_capability=capability_id, title="Policy matrix",
                    input_payload={}, provider_mode="OMNIROUTE",
                )
                assert result.action is SensitiveTaskAction.ALLOW
                assert result.model_tier == worker.model_tier


def test_workforce_policy_redacts_common_personal_identifiers():
    registry = build_default_registry()
    result = evaluate_workforce_policy(
        worker=registry["sonnet"], task_type="DATA_ANALYSIS",
        required_capability="data_analysis", title="Analyze a record",
        input_payload={
            "customer": {
                "full_name": "Example Person",
                "postal_code": "12345",
                "tax_id": "TX-123-45",
                "home_address": "Example Street",
                "status": "active",
            },
        },
        provider_mode="OMNIROUTE",
    )
    record = result.sanitized_input["customer"]
    assert result.action is SensitiveTaskAction.REVIEW
    assert record["full_name"] == "[REDACTED_PERSONAL_DATA]"
    assert record["postal_code"] == "[REDACTED_PERSONAL_DATA]"
    assert record["tax_id"] == "[REDACTED_PERSONAL_DATA]"
    assert record["home_address"] == "[REDACTED_PERSONAL_DATA]"
    assert record["status"] == "active"
    assert result.redacted_fields == 4
