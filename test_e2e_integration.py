
"""
Integration test end-to-end PHASE 1 (tanpa trading, tanpa AI berbayar).

Membuktikan jalur:
  pesan tersimpan -> pipeline (AI di-mock) -> AIAnalysisLog
  -> MANUAL_REVIEW -> TelegramOutbox PENDING -> claim -> decide_review
  -> ApprovalDecision (audit trail).

Semua AI di-mock (analyze_bundle), semua DB sqlite in-memory.
Tidak ada panggilan AI, tidak ada Telegram, tidak ada MT5.
Tidak memproses ulang sinyal historis.
"""

from __future__ import annotations

import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    TelegramMessage,
    AIAnalysisLog,
    ApprovalDecision,
    TelegramOutbox,
)
from app.ai import pipeline
from app.ai import message_aggregator
from app.approval import service as approval_service
from app.approval import outbox


@pytest.fixture
def e2e_db(monkeypatch):
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

    # Semua modul E2E memakai database SQLite yang sama.
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    monkeypatch.setattr(message_aggregator, "SessionLocal", factory)
    monkeypatch.setattr(approval_service, "SessionLocal", factory)
    monkeypatch.setattr(outbox, "SessionLocal", factory)

    # Approver valid agar outbox dibuat.
    monkeypatch.setattr(
        pipeline.settings,
        "TELEGRAM_APPROVER_USER_ID",
        123456789,
    )

    # Semua test menggunakan AI tiruan tanpa transaksi billing.
    monkeypatch.setattr(
        pipeline,
        "get_analysis_usage_status",
        lambda log_id: {
            "analysis_log_id": log_id,
            "total": 0,
            "completed": 0,
            "failed": 0,
            "reserved": 0,
            "unknown": 0,
            "transactions": [],
            "has_uncertain": False,
            "all_terminal": True,
        },
    )

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


def _save_message(
    factory,
    chat_id=-100,
    msg_id=500,
    text="BUY XAUUSD 3920-3922 SL 3900",
):
    from datetime import datetime, timezone

    with factory() as db:
        db.add(
            TelegramMessage(
                chat_id=chat_id,
                telegram_message_id=msg_id,
                message_text=text,
                sender="mentor",
                telegram_date=datetime(
                    2026, 11, 4, 9, 0,
                    tzinfo=timezone.utc,
                ),
                received_at=datetime(
                    2026, 11, 4, 9, 0, 1,
                    tzinfo=timezone.utc,
                ),
                is_edited=False,
            )
        )
        db.commit()


def _mock_analysis(status_inner="CLEAR"):
    """Hasil AI tiruan yang memicu MANUAL_REVIEW di pipeline."""

    return {
        "status": "MANUAL_REVIEW",
        "final_analysis": {
            "status": status_inner,
            "confidence": 0.8,
            "summary": "Sinyal BUY XAUUSD",
            "extracted": {
                "direction": "BUY",
                "entry_zone_text": "3920-3922",
                "stop_loss_text": "3900",
            },
            "evidence": [
                {
                    "message_id": 500,
                    "field": "direction",
                    "quote": "BUY",
                }
            ],
            "uncertainties": [
                "TP belum disebutkan"
            ],
        },
        "attempts": [],
    }


def _mock_analyze(
    bundle,
    resume_state=None,
    checkpoint_callback=None,
):
    """Mock kompatibel dengan signature analyze_bundle terbaru."""

    return _mock_analysis()


def test_e2e_pesan_ke_manual_review_ke_outbox(
    e2e_db,
    monkeypatch,
):
    _save_message(e2e_db)

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        _mock_analyze,
    )

    result = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    assert result["status"] == "MANUAL_REVIEW"

    log_id = result["analysis_log_id"]

    with e2e_db() as db:
        log = db.get(AIAnalysisLog, log_id)

        assert log.status == "MANUAL_REVIEW"

        # Outbox PENDING dibuat dalam transaksi yang sama.
        ob = db.scalar(
            select(TelegramOutbox).where(
                TelegramOutbox.analysis_log_id == log_id
            )
        )

        assert ob is not None
        assert ob.status == "PENDING"
        assert ob.event_key == f"MANUAL_REVIEW:{log_id}"


def test_e2e_dedup_tidak_proses_ulang(
    e2e_db,
    monkeypatch,
):
    _save_message(e2e_db)

    calls = {"n": 0}

    def counting(
        bundle,
        resume_state=None,
        checkpoint_callback=None,
    ):
        calls["n"] += 1
        return _mock_analysis()

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        counting,
    )

    first = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    second = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    assert first["status"] == "MANUAL_REVIEW"
    assert second["status"] == "SKIPPED_DUPLICATE"

    # AI hanya dipanggil sekali.
    assert calls["n"] == 1


def test_e2e_skip_tanpa_teks(
    e2e_db,
    monkeypatch,
):
    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        _mock_analyze,
    )

    result = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=999,
    )

    assert result["status"] == "SKIPPED_NO_TEXT"


def test_e2e_outbox_claim_lalu_approve_audit(
    e2e_db,
    monkeypatch,
):
    _save_message(e2e_db)

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        _mock_analyze,
    )

    result = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    log_id = result["analysis_log_id"]

    # Worker meng-claim outbox secara atomic.
    event = outbox.claim_next_notification()

    assert event is not None
    assert event["analysis_log_id"] == log_id

    with e2e_db() as db:
        ob = db.scalar(
            select(TelegramOutbox).where(
                TelegramOutbox.analysis_log_id == log_id
            )
        )

        assert ob.status == "SENDING"

    # Human decision: APPROVED.
    decision = approval_service.decide_review(
        analysis_log_id=log_id,
        decision="APPROVED",
        reviewer="Approver Uji",
        note="Diperiksa dan disetujui.",
    )

    assert decision["new_status"] == "APPROVED"

    with e2e_db() as db:
        log = db.get(AIAnalysisLog, log_id)

        audit = db.scalar(
            select(ApprovalDecision).where(
                ApprovalDecision.analysis_log_id == log_id
            )
        )

        assert log.status == "APPROVED"
        assert audit is not None
        assert audit.reviewer == "Approver Uji"
        assert audit.previous_status == "MANUAL_REVIEW"


def test_e2e_double_approval_ditolak(
    e2e_db,
    monkeypatch,
):
    _save_message(e2e_db)

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        _mock_analyze,
    )

    result = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    log_id = result["analysis_log_id"]

    approval_service.decide_review(
        analysis_log_id=log_id,
        decision="APPROVED",
        reviewer="Approver 1",
    )

    # Keputusan kedua harus ditolak.
    with pytest.raises(ValueError):
        approval_service.decide_review(
            analysis_log_id=log_id,
            decision="REJECTED",
            reviewer="Approver 2",
            note="Percobaan kedua.",
        )

    with e2e_db() as db:
        audits = db.scalars(
            select(ApprovalDecision).where(
                ApprovalDecision.analysis_log_id == log_id
            )
        ).all()

        assert len(audits) == 1


def test_e2e_evidence_tertelusur_ke_pesan(
    e2e_db,
    monkeypatch,
):
    _save_message(e2e_db)

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        _mock_analyze,
    )

    result = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    log_id = result["analysis_log_id"]

    detail = approval_service.get_review_detail(log_id)

    evidence = detail["final_analysis"]["evidence"]

    bundle_ids = {
        message["telegram_message_id"]
        for message in detail["source_bundle"]["messages"]
    }

    for item in evidence:
        assert item["message_id"] in bundle_ids


def test_e2e_failed_lalu_retry(
    e2e_db,
    monkeypatch,
):
    _save_message(e2e_db)

    # Panggilan pertama: AI melempar error -> FAILED.
    def boom(
        bundle,
        resume_state=None,
        checkpoint_callback=None,
    ):
        raise RuntimeError("AI error simulasi")

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        boom,
    )

    first = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
    )

    assert first["status"] == "FAILED"

    # Retry dengan AI yang sukses.
    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        _mock_analyze,
    )

    retry = pipeline.process_telegram_message(
        chat_id=-100,
        telegram_message_id=500,
        retry_failed=True,
    )

    assert retry["status"] == "MANUAL_REVIEW"
    assert retry["retried"] is True
