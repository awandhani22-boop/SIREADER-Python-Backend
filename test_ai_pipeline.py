import json
import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, AIAnalysisLog
from app.ai import pipeline


CHAT_ID = -1003014486607
MESSAGE_ID = 123


@pytest.fixture
def test_db(monkeypatch):
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
        pipeline,
        "SessionLocal",
        factory,
    )

    # Test standar tidak menggunakan transaksi AI berbayar.
    # Test khusus billing dapat mengganti mock ini.
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


@pytest.fixture
def sample_bundle():
    return {
        "chat_id": CHAT_ID,
        "target_message_id": MESSAGE_ID,
        "messages": [
            {
                "telegram_message_id": MESSAGE_ID,
                "text": "Pantau sell",
                "edit_history": [],
            }
        ],
    }


def add_failed_log(
    factory,
    error="AI tidak mengembalikan JSON valid.",
):
    with factory() as db:
        log = AIAnalysisLog(
            chat_id=CHAT_ID,
            target_message_id=MESSAGE_ID,
            status="FAILED",
            error_message=error,
        )

        db.add(log)
        db.commit()
        db.refresh(log)

        return log.id


def test_pipeline_menyimpan_hasil(
    monkeypatch,
    test_db,
    sample_bundle,
):
    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda chat_id, message_id: sample_bundle,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda bundle, resume_state=None, checkpoint_callback=None: {
            "status": "ANALYZED",
            "final_analysis": {
                "status": "CLEAR",
                "confidence": 0.95,
            },
            "attempts": [
                {"tier": "FAST", "cost_rp": "0.5"}
            ],
        },
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "ANALYZED"
    assert result["retried"] is False

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log is not None
        assert log.status == "ANALYZED"
        assert '"confidence": 0.95' in log.result_json
        assert log.completed_at is not None


def test_pipeline_mencegah_duplikasi(
    monkeypatch,
    test_db,
    sample_bundle,
):
    with test_db() as db:
        db.add(
            AIAnalysisLog(
                chat_id=CHAT_ID,
                target_message_id=MESSAGE_ID,
                status="ANALYZED",
            )
        )
        db.commit()

    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda *args: pytest.fail(
            "Aggregator tidak seharusnya dipanggil."
        ),
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: pytest.fail(
            "AI tidak boleh dipanggil untuk duplikat."
        ),
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "SKIPPED_DUPLICATE"
    assert result["existing_status"] == "ANALYZED"


def test_pipeline_mengabaikan_bundle_kosong(
    monkeypatch,
    test_db,
):
    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda *args: None,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: pytest.fail(
            "AI tidak boleh dipanggil."
        ),
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "SKIPPED_NO_TEXT"

    with test_db() as db:
        logs = db.scalars(
            select(AIAnalysisLog)
        ).all()

        assert len(logs) == 0


def test_pipeline_menyimpan_kegagalan(
    monkeypatch,
    test_db,
    sample_bundle,
):
    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda *args: sample_bundle,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(
            RuntimeError("Simulated AI failure")
        ),
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "FAILED"

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log.status == "FAILED"
        assert "Simulated AI failure" in log.error_message
        assert log.completed_at is not None


def test_retry_failed_berhasil_dengan_flag(
    monkeypatch,
    test_db,
    sample_bundle,
):
    original_id = add_failed_log(test_db)

    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda *args: sample_bundle,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda bundle, resume_state=None, checkpoint_callback=None: {
            "status": "ANALYZED",
            "final_analysis": {
                "status": "CLEAR",
                "confidence": 0.95,
            },
            "attempts": [
                {"tier": "FAST", "cost_rp": "0.5"}
            ],
        },
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
        retry_failed=True,
    )

    assert result["status"] == "ANALYZED"
    assert result["retried"] is True
    assert result["analysis_log_id"] == original_id

    with test_db() as db:
        logs = db.scalars(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        ).all()

        assert len(logs) == 1
        assert logs[0].id == original_id
        assert logs[0].status == "ANALYZED"
        assert "AI tidak mengembalikan JSON valid" in (
            logs[0].error_message
        )
        assert logs[0].result_json is not None


def test_failed_tidak_diulang_tanpa_flag(
    monkeypatch,
    test_db,
    sample_bundle,
):
    original_id = add_failed_log(test_db)

    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda *args: pytest.fail(
            "Aggregator tidak boleh dipanggil."
        ),
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: pytest.fail(
            "AI tidak boleh dipanggil."
        ),
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "SKIPPED_DUPLICATE"
    assert result["analysis_log_id"] == original_id
    assert result["existing_status"] == "FAILED"


def test_retry_gagal_mempertahankan_riwayat_error(
    monkeypatch,
    test_db,
    sample_bundle,
):
    original_id = add_failed_log(test_db)

    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda *args: sample_bundle,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(
            RuntimeError("Retry masih gagal.")
        ),
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
        retry_failed=True,
    )

    assert result["status"] == "FAILED"
    assert result["retried"] is True

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log.id == original_id
        assert log.status == "FAILED"
        assert "AI tidak mengembalikan JSON valid" in (
            log.error_message
        )
        assert "Retry masih gagal" in log.error_message


def _mock_usage(transactions):
    statuses = [item["status"] for item in transactions]

    return {
        "total": len(transactions),
        "completed": statuses.count("COMPLETED"),
        "failed": statuses.count("FAILED"),
        "reserved": statuses.count("RESERVED"),
        "unknown": statuses.count("UNKNOWN"),
        "transactions": transactions,
        "has_uncertain": any(
            status in ("RESERVED", "UNKNOWN")
            for status in statuses
        ),
        "all_terminal": all(
            status in ("COMPLETED", "FAILED")
            for status in statuses
        ),
    }


def _make_checkpoint_result(request_ids):
    resume_state = {
        "version": 1,
        "next_tier": "REASONING",
        "path": ["FAST"],
        "fast_analysis": {
            "status": "CLEAR",
            "confidence": 0.95,
        },
        "attempts": [
            {
                "tier": "FAST",
                "request_id": request_id,
            }
            for request_id in request_ids
        ],
    }

    return json.dumps({
        "_type": pipeline.CHECKPOINT_TYPE,
        "resume_state": resume_state,
    })


def test_retry_diblokir_completed_tanpa_checkpoint(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "get_analysis_usage_status",
        lambda log_id: _mock_usage([
            {
                "request_id": "req-completed-001",
                "tier": "FAST",
                "status": "COMPLETED",
            }
        ]),
    )

    result = pipeline.check_retry_safety(
        1,
        None,
    )

    assert result["safe"] is False
    assert (
        result["reason"]
        == "COMPLETED_TRANSACTION_WITHOUT_CHECKPOINT"
    )


def test_retry_diblokir_checkpoint_billing_mismatch(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "get_analysis_usage_status",
        lambda log_id: _mock_usage([
            {
                "request_id": "req-completed-001",
                "tier": "FAST",
                "status": "COMPLETED",
            }
        ]),
    )

    checkpoint = _make_checkpoint_result([
        "req-different-999",
    ])

    result = pipeline.check_retry_safety(
        1,
        checkpoint,
    )

    assert result["safe"] is False
    assert result["reason"] == "CHECKPOINT_BILLING_MISMATCH"


@pytest.mark.parametrize(
    "transaction_status",
    ["RESERVED", "UNKNOWN"],
)
def test_retry_diblokir_transaksi_belum_pasti(
    monkeypatch,
    transaction_status,
):
    monkeypatch.setattr(
        pipeline,
        "get_analysis_usage_status",
        lambda log_id: _mock_usage([
            {
                "request_id": "req-uncertain-001",
                "tier": "FAST",
                "status": transaction_status,
            }
        ]),
    )

    result = pipeline.check_retry_safety(
        1,
        None,
    )

    assert result["safe"] is False
    assert result["reason"] == "UNCERTAIN_BILLING_TRANSACTION"


def test_retry_diizinkan_jika_checkpoint_cocok(monkeypatch):
    request_id = "req-completed-001"

    monkeypatch.setattr(
        pipeline,
        "get_analysis_usage_status",
        lambda log_id: _mock_usage([
            {
                "request_id": request_id,
                "tier": "FAST",
                "status": "COMPLETED",
            }
        ]),
    )

    checkpoint = _make_checkpoint_result([
        request_id,
    ])

    result = pipeline.check_retry_safety(
        1,
        checkpoint,
    )

    assert result["safe"] is True
    assert result["resume_state"] is not None
    assert result["resume_state"]["attempts"][0]["request_id"] == (
        request_id
    )

def test_pipeline_mengintegrasikan_decision_service(
    monkeypatch,
    test_db,
    sample_bundle,
):
    calls = {}

    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda chat_id, message_id: sample_bundle,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: {
            "status": "ANALYZED",
            "final_analysis": {
                "status": "CLEAR",
                "confidence": 0.95,
            },
            "attempts": [],
        },
    )

    def fake_decision(
        ai_result,
        *,
        bundle,
        analysis_log_id,
        session_factory=None,
    ):
        calls["ai_result"] = ai_result
        calls["bundle"] = bundle
        calls["analysis_log_id"] = analysis_log_id
        calls["session_factory"] = session_factory

        return {
            "status": "READY_FOR_APPROVAL",
            "reason": "TEST_APPROVAL",
            "order_sent": False,
        }

    def fake_map(decision):
        assert decision["status"] == "READY_FOR_APPROVAL"
        return "MANUAL_REVIEW"

    monkeypatch.setattr(
        pipeline,
        "evaluate_pipeline_decision",
        fake_decision,
    )

    monkeypatch.setattr(
        pipeline,
        "map_decision_to_analysis_status",
        fake_map,
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "MANUAL_REVIEW"
    assert result["analysis"]["decision"]["status"] == (
        "READY_FOR_APPROVAL"
    )

    assert calls["bundle"] is sample_bundle
    assert calls["analysis_log_id"] == result["analysis_log_id"]
    assert calls["session_factory"] is pipeline.SessionLocal

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log.status == "MANUAL_REVIEW"

        saved = json.loads(log.result_json)

        assert saved["decision"]["status"] == (
            "READY_FOR_APPROVAL"
        )
        assert saved["decision"]["order_sent"] is False


def test_pipeline_decision_service_error_fail_closed(
    monkeypatch,
    test_db,
    sample_bundle,
):
    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda chat_id, message_id: sample_bundle,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: {
            "status": "ANALYZED",
            "final_analysis": {
                "status": "CLEAR",
                "confidence": 0.95,
            },
            "attempts": [],
        },
    )

    def failing_decision(*args, **kwargs):
        raise RuntimeError("Simulated decision failure")

    monkeypatch.setattr(
        pipeline,
        "evaluate_pipeline_decision",
        failing_decision,
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "MANUAL_REVIEW"

    decision = result["analysis"]["decision"]

    assert decision["status"] == "NEEDS_REVIEW"
    assert decision["reason"] == "DECISION_SERVICE_FAILED"
    assert decision["order_sent"] is False

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log.status == "MANUAL_REVIEW"

        saved = json.loads(log.result_json)

        assert saved["decision"]["reason"] == (
            "DECISION_SERVICE_FAILED"
        )

# ============================================================
# TRADE DECISION WATCH PIPELINE INTEGRATION
# ============================================================

def _seed_pipeline_watch_episode(factory):
    from datetime import datetime, timezone

    from app.database.models import (
        TelegramMessage,
        TradeEpisode,
    )

    now = datetime.now(timezone.utc)
    episode_key = f"trade_{CHAT_ID}_{MESSAGE_ID}"

    with factory() as db:
        message = TelegramMessage(
            chat_id=CHAT_ID,
            telegram_message_id=MESSAGE_ID,
            sender="mentor-test",
            message_text="BUY XAUUSD 4100-4102",
            received_at=now,
            telegram_date=now,
            is_edited=False,
        )
        db.add(message)
        db.flush()

        episode = TradeEpisode(
            episode_key=episode_key,
            chat_id=CHAT_ID,
            anchor_message_id=message.id,
            direction="BUY",
            entry_low="4100",
            entry_high="4102",
            entry_midpoint="4101",
            status="OPEN",
        )
        db.add(episode)
        db.commit()
        db.refresh(episode)

        return episode_key, episode.id


def _configure_waiting_spread_pipeline(
    monkeypatch,
    sample_bundle,
    episode_key,
):
    decision = {
        "status": "WAITING_FOR_SPREAD",
        "reason": "SPREAD_EXCEEDS_STOP_LOSS",
        "trade_episode_id": episode_key,
        "maximum_spread_pips": "30",
        "order_sent": False,
        "market_data": {
            "available": True,
            "symbol": "XAUUSD",
            "bid": "4100.00",
            "ask": "4103.10",
            "spread_pips": "31",
            "readonly": True,
            "account_mode": "DEMO",
        },
    }

    watch_payload = {
        "trade_episode_key": episode_key,
        "symbol": "XAUUSD",
        "maximum_spread_pips": "30",
        "bid": "4100.00",
        "ask": "4103.10",
        "broker_server": None,
    }

    monkeypatch.setattr(
        pipeline,
        "collect_message_bundle",
        lambda chat_id, message_id: sample_bundle,
    )

    # Episode sudah dibuat oleh helper test.
    # Hindari sinkronisasi ulang agar test fokus pada watch.
    monkeypatch.setattr(
        pipeline,
        "persist_bundle_trade_episodes",
        lambda *args, **kwargs: None,
    )

    monkeypatch.setattr(
        pipeline,
        "analyze_bundle",
        lambda *args, **kwargs: {
            "status": "ANALYZED",
            "final_analysis": {
                "status": "CLEAR",
                "confidence": 0.95,
            },
            "attempts": [],
        },
    )

    monkeypatch.setattr(
        pipeline,
        "evaluate_pipeline_decision",
        lambda *args, **kwargs: dict(decision),
    )

    monkeypatch.setattr(
        pipeline,
        "map_decision_to_analysis_status",
        lambda result: "ANALYZED",
    )

    return watch_payload


def test_pipeline_waiting_for_spread_menyimpan_watch_atomik(
    monkeypatch,
    test_db,
    sample_bundle,
):
    from sqlalchemy import func

    from app.database.models import (
        TelegramOutbox,
        TradeDecisionWatch,
    )

    episode_key, episode_id = _seed_pipeline_watch_episode(
        test_db
    )

    watch_payload = _configure_waiting_spread_pipeline(
        monkeypatch,
        sample_bundle,
        episode_key,
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "ANALYZED"
    assert result["analysis"]["decision"]["status"] == (
        "WAITING_FOR_SPREAD"
    )
    assert result["analysis"]["decision"]["order_sent"] is False

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log is not None
        assert log.status == "ANALYZED"

        watch = db.scalar(
            select(TradeDecisionWatch).where(
                TradeDecisionWatch.analysis_log_id == log.id
            )
        )

        assert watch is not None
        assert watch.trade_episode_id == episode_id
        assert watch.symbol == "XAUUSD"
        assert watch.status == "WAITING_FOR_SPREAD"
        assert float(watch.spread_pips) == pytest.approx(31)
        assert float(watch.maximum_spread_pips) == pytest.approx(30)

        outbox_count = db.scalar(
            select(func.count()).select_from(TelegramOutbox)
        )
        assert outbox_count == 0

        original_watch_id = watch.id

    # Pembuatan ulang untuk analysis log yang sama harus idempotent.
    pipeline.finish_analysis_log(
        log_id=result["analysis_log_id"],
        status="ANALYZED",
        result=result["analysis"],
        decision_watch=watch_payload,
    )

    with test_db() as db:
        watches = db.scalars(
            select(TradeDecisionWatch).where(
                TradeDecisionWatch.analysis_log_id
                == result["analysis_log_id"]
            )
        ).all()

        assert len(watches) == 1
        assert watches[0].id == original_watch_id


def test_pipeline_watch_persistence_error_fail_closed(
    monkeypatch,
    test_db,
    sample_bundle,
):
    from sqlalchemy import func

    from app.database.models import TradeDecisionWatch

    episode_key, episode_id = _seed_pipeline_watch_episode(
        test_db
    )

    _configure_waiting_spread_pipeline(
        monkeypatch,
        sample_bundle,
        episode_key,
    )

    def fail_watch_creation(*args, **kwargs):
        raise RuntimeError("Simulated watch persistence failure")

    monkeypatch.setattr(
        pipeline,
        "create_trade_decision_watch",
        fail_watch_creation,
    )

    result = pipeline.process_telegram_message(
        CHAT_ID,
        MESSAGE_ID,
    )

    assert result["status"] == "MANUAL_REVIEW"

    decision = result["analysis"]["decision"]

    assert decision["status"] == "NEEDS_REVIEW"
    assert decision["reason"] == (
        "TRADE_DECISION_WATCH_PERSISTENCE_FAILED"
    )
    assert decision["order_sent"] is False

    with test_db() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == CHAT_ID,
                AIAnalysisLog.target_message_id == MESSAGE_ID,
            )
        )

        assert log is not None
        assert log.status == "MANUAL_REVIEW"

        saved = json.loads(log.result_json)
        assert saved["decision"]["reason"] == (
            "TRADE_DECISION_WATCH_PERSISTENCE_FAILED"
        )

        watch_count = db.scalar(
            select(func.count()).select_from(TradeDecisionWatch)
        )
        assert watch_count == 0
