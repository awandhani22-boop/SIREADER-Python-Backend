import json
import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    AIAnalysisLog,
    ApprovalDecision,
)
from app.approval import service


@pytest.fixture
def approval_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)

    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )

    monkeypatch.setattr(
        service,
        "SessionLocal",
        factory,
    )

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


def add_analysis(
    factory,
    status="MANUAL_REVIEW",
    message_id=123,
):
    result = {
        "status": "ANALYZED",
        "final_analysis": {
            "status": "CLEAR",
            "confidence": 0.95,
            "summary": "Sinyal BUY",
            "extracted": {
                "direction": "BUY",
                "entry_zone_text": "3922-3920",
                "explicit_entry_text": None,
                "stop_loss_text": "Sl20/30 pips",
                "take_profit_texts": [],
                "conditions": [],
                "action_instructions": [],
            },
            "evidence": [
                {
                    "message_id": message_id,
                    "field": "direction",
                    "quote": "BUY",
                }
            ],
            "uncertainties": [
                "Take profit tidak disebutkan."
            ],
        },
        "attempts": [],
    }

    with factory() as db:
        log = AIAnalysisLog(
            chat_id=-1003014486607,
            target_message_id=message_id,
            status=status,
            result_json=json.dumps(result),
        )

        db.add(log)
        db.commit()
        db.refresh(log)

        return log.id


def test_daftar_hanya_menampilkan_manual_review(approval_db):
    add_analysis(approval_db, "MANUAL_REVIEW", 123)
    add_analysis(approval_db, "ANALYZED", 124)
    add_analysis(approval_db, "FAILED", 125)

    result = service.list_pending_reviews()

    assert len(result) == 1
    assert result[0]["target_message_id"] == 123
    assert result[0]["direction"] == "BUY"
    assert result[0]["uncertainties"]


def test_detail_mengandung_evidence_dan_sumber(
    monkeypatch,
    approval_db,
):
    log_id = add_analysis(approval_db)

    monkeypatch.setattr(
        service,
        "collect_message_bundle",
        lambda **kwargs: {
            "target_message_id": 123,
            "messages": [
                {"telegram_message_id": 123, "text": "BUY"}
            ],
        },
    )

    detail = service.get_review_detail(log_id)

    assert detail["status"] == "MANUAL_REVIEW"
    assert detail["final_analysis"]["confidence"] == 0.95
    assert detail["final_analysis"]["evidence"][0]["quote"] == "BUY"
    assert detail["source_bundle"]["target_message_id"] == 123


def test_approve_mengubah_status_dan_membuat_audit(
    approval_db,
):
    log_id = add_analysis(approval_db)

    result = service.decide_review(
        analysis_log_id=log_id,
        decision="APPROVED",
        reviewer="Reviewer 1",
        note="Informasi sudah saya periksa.",
    )

    assert result["new_status"] == "APPROVED"

    with approval_db() as db:
        log = db.get(AIAnalysisLog, log_id)
        decisions = db.scalars(
            select(ApprovalDecision).where(
                ApprovalDecision.analysis_log_id == log_id
            )
        ).all()

        assert log.status == "APPROVED"
        assert len(decisions) == 1
        assert decisions[0].reviewer == "Reviewer 1"


def test_reject_wajib_memiliki_alasan(approval_db):
    log_id = add_analysis(approval_db)

    with pytest.raises(ValueError, match="Alasan penolakan wajib"):
        service.decide_review(
            analysis_log_id=log_id,
            decision="REJECTED",
            reviewer="Reviewer 1",
        )

    with approval_db() as db:
        log = db.get(AIAnalysisLog, log_id)
        assert log.status == "MANUAL_REVIEW"


def test_reject_berhasil_dengan_alasan(approval_db):
    log_id = add_analysis(approval_db)

    result = service.decide_review(
        analysis_log_id=log_id,
        decision="REJECTED",
        reviewer="Reviewer 1",
        note="Bukti entry belum cukup.",
    )

    assert result["new_status"] == "REJECTED"

    with approval_db() as db:
        log = db.get(AIAnalysisLog, log_id)
        audit = db.scalar(
            select(ApprovalDecision).where(
                ApprovalDecision.analysis_log_id == log_id
            )
        )

        assert log.status == "REJECTED"
        assert audit.note == "Bukti entry belum cukup."


def test_keputusan_kedua_ditolak(approval_db):
    log_id = add_analysis(approval_db)

    service.decide_review(
        analysis_log_id=log_id,
        decision="APPROVED",
        reviewer="Reviewer 1",
    )

    with pytest.raises(ValueError, match="Status analisis saat ini"):
        service.decide_review(
            analysis_log_id=log_id,
            decision="REJECTED",
            reviewer="Reviewer 2",
            note="Percobaan keputusan kedua.",
        )

    with approval_db() as db:
        decisions = db.scalars(
            select(ApprovalDecision).where(
                ApprovalDecision.analysis_log_id == log_id
            )
        ).all()

        assert len(decisions) == 1


def test_status_failed_tidak_boleh_diapprove(approval_db):
    log_id = add_analysis(approval_db, status="FAILED")

    with pytest.raises(ValueError, match="Status analisis saat ini"):
        service.decide_review(
            analysis_log_id=log_id,
            decision="APPROVED",
            reviewer="Reviewer 1",
        )


def test_keputusan_tidak_valid_ditolak(approval_db):
    log_id = add_analysis(approval_db)

    with pytest.raises(ValueError, match="APPROVED atau REJECTED"):
        service.decide_review(
            analysis_log_id=log_id,
            decision="EXECUTE",
            reviewer="Reviewer 1",
        )
