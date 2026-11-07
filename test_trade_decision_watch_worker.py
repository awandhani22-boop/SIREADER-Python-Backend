import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    AIAnalysisLog,
    Base,
    TelegramMessage,
    TelegramOutbox,
    TradeDecisionWatch,
    TradeEpisode,
)
from app.signals import trade_decision_watch_repository as repository
from app.signals import trade_decision_watch_worker as worker


def normalize_utc(value):
    """SQLite may return DateTime(timezone=True) as naive UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


CHAT_ID = -1003014486607
MESSAGE_ID = 98765


@pytest.fixture
def worker_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def seed_watch(factory, *, episode_status="OPEN", now=None):
    now = now or datetime.now(timezone.utc)
    episode_key = f"trade_{CHAT_ID}_{MESSAGE_ID}"
    ai_result = {
        "status": "ANALYZED",
        "final_analysis": {
            "status": "CLEAR",
            "confidence": 0.95,
            "extracted": {
                "direction": "BUY",
                "entry_zone_text": "4100-4102",
                "explicit_entry_text": None,
                "stop_loss_text": None,
                "take_profit_texts": ["TP 100 pips"],
                "conditions": [],
                "action_instructions": [],
            },
        },
        "attempts": [],
        "decision": {
            "status": "WAITING_FOR_SPREAD",
            "reason": "SPREAD_EXCEEDS_STOP_LOSS",
            "order_sent": False,
        },
    }

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
        log = AIAnalysisLog(
            chat_id=CHAT_ID,
            target_message_id=MESSAGE_ID,
            status="ANALYZED",
            result_json=json.dumps(ai_result),
        )
        db.add_all([episode, log])
        db.flush()

        watch = repository.create_watch(
            db,
            analysis_log_id=log.id,
            trade_episode_id=episode.id,
            symbol="XAUUSD",
            broker_server="HFMarketsGlobal-Demo4",
            maximum_spread_pips="30",
            bid="4100.00",
            ask="4103.10",
            now=now - timedelta(seconds=10),
            next_check_at=now - timedelta(seconds=1),
        )
        db.commit()
        if episode_status != "OPEN":
            episode.status = episode_status
            db.commit()
        return {
            "watch_id": watch.id,
            "analysis_log_id": log.id,
            "episode_id": episode.id,
            "episode_key": episode_key,
            "ai_result": ai_result,
        }


def quote(*, bid="4100.00", ask="4103.10", now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "ok": True,
        "symbol": "XAUUSD",
        "bid": bid,
        "ask": ask,
        "tick_size": "0.01",
        "tick_time": int(now.timestamp()),
        "account_mode": "DEMO",
        "readonly": True,
        "broker_server": "HFMarketsGlobal-Demo4",
    }


def make_decision(status, *, bid="4100.00", ask="4103.10"):
    return {
        "status": status,
        "reason": f"TEST_{status}",
        "maximum_spread_pips": "30",
        "market_data": {
            "available": True,
            "symbol": "XAUUSD",
            "bid": bid,
            "ask": ask,
            "spread_pips": "31" if status == "WAITING_FOR_SPREAD" else "2",
            "readonly": True,
            "account_mode": "DEMO",
        },
        "order_sent": False,
    }


def fake_bundle(chat_id, message_id):
    return {
        "chat_id": chat_id,
        "target_message_id": message_id,
        "messages": [
            {
                "telegram_message_id": message_id,
                "text": "BUY XAUUSD 4100-4102",
                "message_roles": ["NEW_SIGNAL"],
                "trade_episode_id": f"trade_{chat_id}_{message_id}",
            }
        ],
    }


def test_worker_spread_tetap_tinggi_menjadwalkan_ulang(worker_db):
    now = datetime.now(timezone.utc)
    records = seed_watch(worker_db, now=now)

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=lambda symbol, server: quote(now=now),
        bundle_loader=fake_bundle,
        decision_evaluator=lambda *args, **kwargs: make_decision(
            "WAITING_FOR_SPREAD"
        ),
        now=now,
    )

    assert result["status"] == repository.WATCH_WAITING
    with worker_db() as db:
        watch = db.get(TradeDecisionWatch, records["watch_id"])
        assert watch.status == repository.WATCH_WAITING
        assert watch.check_count == 1
        assert normalize_utc(watch.next_check_at) > now
        assert watch.last_reason == "SPREAD_EXCEEDS_STOP_LOSS"


def test_worker_spread_normal_resolve_dan_membuat_outbox(worker_db, monkeypatch):
    now = datetime.now(timezone.utc)
    records = seed_watch(worker_db, now=now)
    monkeypatch.setattr(worker, "_get_approver_user_id", lambda: 123456)

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=lambda symbol, server: quote(
            bid="4100.00", ask="4100.20", now=now
        ),
        bundle_loader=fake_bundle,
        decision_evaluator=lambda *args, **kwargs: make_decision(
            "READY_FOR_APPROVAL",
            bid="4100.00",
            ask="4100.20",
        ),
        status_mapper=lambda decision: "MANUAL_REVIEW",
        now=now,
    )

    assert result["status"] == repository.WATCH_RESOLVED
    assert result["analysis_status"] == "MANUAL_REVIEW"

    with worker_db() as db:
        watch = db.get(TradeDecisionWatch, records["watch_id"])
        log = db.get(AIAnalysisLog, records["analysis_log_id"])
        outbox = db.scalar(
            select(TelegramOutbox).where(
                TelegramOutbox.event_key
                == f"MANUAL_REVIEW:{records['analysis_log_id']}"
            )
        )
        assert watch.status == repository.WATCH_RESOLVED
        assert log.status == "MANUAL_REVIEW"
        assert outbox is not None
        assert outbox.status == "PENDING"
        saved = json.loads(log.result_json)
        assert saved["decision"]["status"] == "READY_FOR_APPROVAL"
        assert saved["decision"]["order_sent"] is False


def test_worker_mt5_error_retry_tanpa_terminalisasi(worker_db):
    now = datetime.now(timezone.utc)
    records = seed_watch(worker_db, now=now)

    def broken_quote(symbol, server):
        raise RuntimeError("terminal temporarily unavailable")

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=broken_quote,
        bundle_loader=fake_bundle,
        now=now,
        retry_seconds=7,
    )

    assert result["status"] == "RETRY_SCHEDULED"
    with worker_db() as db:
        watch = db.get(TradeDecisionWatch, records["watch_id"])
        assert watch.status == repository.WATCH_WAITING
        assert normalize_utc(watch.next_check_at) > now
        assert watch.last_reason.startswith("RECHECK_RETRY:")


def test_worker_episode_non_open_membatalkan_watch(worker_db):
    now = datetime.now(timezone.utc)
    records = seed_watch(
        worker_db,
        episode_status="SKIP_REQUESTED",
        now=now,
    )

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=lambda *args: pytest.fail("MT5 tidak boleh dibaca"),
        bundle_loader=lambda *args: pytest.fail("Bundle tidak perlu dibaca"),
        now=now,
    )

    assert result["status"] == repository.WATCH_SKIPPED
    with worker_db() as db:
        watch = db.get(TradeDecisionWatch, records["watch_id"])
        log = db.get(AIAnalysisLog, records["analysis_log_id"])
        assert watch.status == repository.WATCH_SKIPPED
        assert log.status == "ANALYZED"
        saved = json.loads(log.result_json)
        assert saved["decision"]["status"] == "SKIPPED"
        assert saved["decision"]["reason"] == (
            "EPISODE_NOT_OPEN:SKIP_REQUESTED"
        )


def test_worker_menolak_watch_yang_belum_jatuh_tempo(worker_db):
    now = datetime.now(timezone.utc)
    records = seed_watch(worker_db, now=now)

    with worker_db() as db:
        watch = db.get(TradeDecisionWatch, records["watch_id"])
        watch.next_check_at = now + timedelta(minutes=5)
        db.commit()

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=lambda *args: pytest.fail("Watch belum due"),
        bundle_loader=lambda *args: pytest.fail("Watch belum due"),
        now=now,
    )
    assert result["status"] == "NOT_CLAIMED"


def test_worker_kehilangan_claim_tidak_mengubah_watch(worker_db):
    now = datetime.now(timezone.utc)
    records = seed_watch(worker_db, now=now)

    with worker_db() as db:
        first = repository.claim_due_watch(
            db,
            records["watch_id"],
            now=now,
            lease_seconds=60,
        )
        assert first is not None
        db.commit()

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=lambda *args: pytest.fail("Watch sudah diklaim"),
        bundle_loader=lambda *args: pytest.fail("Watch sudah diklaim"),
        now=now,
    )

    assert result["status"] == "NOT_CLAIMED"


def test_worker_market_data_stale_menjadwalkan_ulang(worker_db):
    now = datetime.now(timezone.utc)
    records = seed_watch(worker_db, now=now)
    stale_decision = make_decision("NEEDS_REVIEW")
    stale_decision["market_data"]["available"] = False
    stale_decision["market_data"]["reason"] = "MT5_TICK_STALE"

    result = worker.process_watch(
        records["watch_id"],
        session_factory=worker_db,
        quote_reader=lambda symbol, server: quote(now=now),
        bundle_loader=fake_bundle,
        decision_evaluator=lambda *args, **kwargs: stale_decision,
        now=now,
    )

    assert result["status"] == "RETRY_SCHEDULED"
    with worker_db() as db:
        watch = db.get(TradeDecisionWatch, records["watch_id"])
        assert watch.status == repository.WATCH_WAITING
        assert watch.last_reason.startswith("MT5_FRESHNESS_RETRY:")
