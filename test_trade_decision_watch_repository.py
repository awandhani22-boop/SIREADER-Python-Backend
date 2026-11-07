from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    AIAnalysisLog,
    TelegramMessage,
    TradeEpisode,
    TradeDecisionWatch,
)
from app.signals import trade_decision_watch_repository as repository


CHAT_ID = -1003014486607
OTHER_CHAT_ID = -1009999999999


@pytest.fixture
def watch_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def create_parent_records(
    factory,
    *,
    message_id=1001,
    episode_chat_id=CHAT_ID,
    analysis_chat_id=CHAT_ID,
    episode_status="OPEN",
):
    now = datetime.now(timezone.utc)
    with factory() as db:
        message = TelegramMessage(
            chat_id=episode_chat_id,
            telegram_message_id=message_id,
            sender="mentor-test",
            message_text="BUY XAUUSD 4100-4102",
            received_at=now,
            telegram_date=now,
            is_edited=False,
        )
        db.add(message)
        db.flush()
        episode = TradeEpisode(
            episode_key=f"trade_{episode_chat_id}_{message_id}",
            chat_id=episode_chat_id,
            anchor_message_id=message.id,
            direction="BUY",
            entry_low=Decimal("4100"),
            entry_high=Decimal("4102"),
            entry_midpoint=Decimal("4101"),
            status=episode_status,
        )
        log = AIAnalysisLog(
            chat_id=analysis_chat_id,
            target_message_id=message_id,
            status="ANALYZED",
        )
        db.add_all([episode, log])
        db.commit()
        return {
            "episode_id": episode.id,
            "analysis_log_id": log.id,
        }


def create_test_watch(
    db,
    records,
    *,
    now=None,
    next_check_at=None,
    symbol="XAUUSD",
    broker_server="HFMarketsGlobal-Demo4",
    maximum_spread_pips="30",
    bid="4098.00",
    ask="4101.10",
):
    return repository.create_watch(
        db,
        analysis_log_id=records["analysis_log_id"],
        trade_episode_id=records["episode_id"],
        symbol=symbol,
        broker_server=broker_server,
        maximum_spread_pips=maximum_spread_pips,
        bid=bid,
        ask=ask,
        now=now,
        next_check_at=next_check_at,
    )


def count_watches(db):
    return db.scalar(
        select(func.count()).select_from(TradeDecisionWatch)
    )


def test_create_watch_sukses(watch_db):
    records = create_parent_records(watch_db)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        watch = create_test_watch(db, records, now=now)
        db.commit()
        assert watch.id is not None
        assert watch.status == "WAITING_FOR_SPREAD"
        assert watch.symbol == "XAUUSD"
        assert watch.broker_server == "HFMarketsGlobal-Demo4"
        assert watch.spread_pips == Decimal("31.00")
        assert watch.maximum_spread_pips == Decimal("30")
        assert watch.check_count == 0
        assert watch.last_reason == "SPREAD_EXCEEDS_STOP_LOSS"


def test_create_watch_idempotent(watch_db):
    records = create_parent_records(watch_db)
    with watch_db() as db:
        first = create_test_watch(db, records)
        second = create_test_watch(db, records)
        assert first.id == second.id
        assert count_watches(db) == 1
        db.commit()


def test_create_watch_menolak_identitas_berbeda(watch_db):
    records = create_parent_records(watch_db)
    with watch_db() as db:
        create_test_watch(db, records)
        with pytest.raises(repository.TradeDecisionWatchConflictError):
            create_test_watch(db, records, symbol="XAUUSD247")


def test_create_watch_menolak_spread_normal(watch_db):
    records = create_parent_records(watch_db)
    with watch_db() as db:
        with pytest.raises(repository.TradeDecisionWatchValidationError):
            create_test_watch(
                db, records, bid="4101.00", ask="4101.20"
            )


@pytest.mark.parametrize("episode_status", ["SKIP_REQUESTED", "CLOSED"])
def test_create_watch_menolak_episode_non_open(watch_db, episode_status):
    records = create_parent_records(
        watch_db, episode_status=episode_status
    )
    with watch_db() as db:
        with pytest.raises(repository.TradeDecisionWatchConflictError):
            create_test_watch(db, records)


def test_create_watch_menolak_chat_berbeda(watch_db):
    records = create_parent_records(
        watch_db,
        episode_chat_id=CHAT_ID,
        analysis_chat_id=OTHER_CHAT_ID,
    )
    with watch_db() as db:
        with pytest.raises(repository.TradeDecisionWatchConflictError):
            create_test_watch(db, records)


def test_get_watch_by_id_dan_analysis_log(watch_db):
    records = create_parent_records(watch_db)
    with watch_db() as db:
        created = create_test_watch(db, records)
        db.commit()
        by_id = repository.get_watch_by_id(db, created.id)
        by_log = repository.get_watch_by_analysis_log(
            db, records["analysis_log_id"]
        )
        assert by_id is not None
        assert by_log is not None
        assert by_id.id == by_log.id


def test_get_due_watches_hanya_mengambil_yang_jatuh_tempo(watch_db):
    first_records = create_parent_records(watch_db, message_id=1001)
    second_records = create_parent_records(watch_db, message_id=1002)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        due = create_test_watch(
            db,
            first_records,
            now=now - timedelta(seconds=5),
            next_check_at=now - timedelta(seconds=1),
        )
        future = create_test_watch(
            db,
            second_records,
            now=now,
            next_check_at=now + timedelta(minutes=5),
        )
        db.commit()
        rows = repository.get_due_watches(db, now=now)
        assert [item.id for item in rows] == [due.id]
        assert future.id not in [item.id for item in rows]


def test_update_watch_spread_tetap_tinggi(watch_db):
    records = create_parent_records(watch_db)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        watch = create_test_watch(db, records, now=now)
        updated = repository.update_watch_check(
            db,
            watch.id,
            bid="4098.00",
            ask="4101.10",
            checked_at=now + timedelta(seconds=2),
        )
        db.commit()
        assert updated.status == "WAITING_FOR_SPREAD"
        assert updated.spread_pips == Decimal("31.00")
        assert updated.check_count == 1
        assert updated.last_reason == "SPREAD_EXCEEDS_STOP_LOSS"


def test_update_watch_spread_normal_masuk_rechecking(watch_db):
    records = create_parent_records(watch_db)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        watch = create_test_watch(db, records, now=now)
        updated = repository.update_watch_check(
            db,
            watch.id,
            bid="4101.00",
            ask="4101.20",
            checked_at=now + timedelta(seconds=2),
        )
        db.commit()
        assert updated.status == "RECHECKING"
        assert updated.spread_pips == Decimal("2.00")
        assert updated.check_count == 1
        assert updated.last_reason == "SPREAD_NORMAL_REEVALUATION_REQUIRED"


def test_update_watch_spread_tepat_batas_diperbolehkan(watch_db):
    records = create_parent_records(watch_db)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        watch = create_test_watch(db, records, now=now)
        updated = repository.update_watch_check(
            db,
            watch.id,
            bid="4100.00",
            ask="4103.00",
            checked_at=now + timedelta(seconds=2),
        )
        assert updated.spread_pips == Decimal("30")
        assert updated.status == "RECHECKING"
        db.commit()


def test_update_watch_menolak_quote_invalid(watch_db):
    records = create_parent_records(watch_db)
    with watch_db() as db:
        watch = create_test_watch(db, records)
        with pytest.raises(repository.TradeDecisionWatchValidationError):
            repository.update_watch_check(
                db, watch.id, bid="4102", ask="4101"
            )


def test_resolve_watch_tidak_bisa_diperbarui_lagi(watch_db):
    records = create_parent_records(watch_db)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        watch = create_test_watch(db, records)
        resolved = repository.resolve_watch(
            db,
            watch.id,
            reason="DECISION_REEVALUATED",
            now=now,
        )
        db.commit()
        assert resolved.status == "RESOLVED"
        assert resolved.resolved_at is not None
        with pytest.raises(repository.TradeDecisionWatchConflictError):
            repository.update_watch_check(
                db,
                watch.id,
                bid="4101",
                ask="4101.2",
                checked_at=now + timedelta(seconds=1),
            )


def test_terminal_watch_tidak_bisa_diaktifkan_ulang(watch_db):
    records = create_parent_records(watch_db)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        watch = create_test_watch(db, records)
        repository.skip_watch(
            db,
            watch.id,
            reason="ENTRY_PRICE_TOO_FAR_FROM_MIDPOINT",
            now=now,
        )
        db.commit()
        with pytest.raises(repository.TradeDecisionWatchConflictError):
            create_test_watch(db, records, now=now + timedelta(seconds=5))


def test_cancel_expire_dan_error_transisi(watch_db):
    records = create_parent_records(watch_db, message_id=1001)
    second = create_parent_records(watch_db, message_id=1002)
    third = create_parent_records(watch_db, message_id=1003)
    now = datetime.now(timezone.utc)
    with watch_db() as db:
        first_watch = create_test_watch(db, records)
        second_watch = create_test_watch(db, second)
        third_watch = create_test_watch(db, third)
        repository.cancel_watch(
            db, first_watch.id, reason="MENTOR_CANCELLED_EPISODE", now=now
        )
        repository.expire_watch(db, second_watch.id, now=now)
        repository.mark_watch_error(
            db, third_watch.id, reason="MONITOR_READ_FAILED", now=now
        )
        db.commit()
        assert first_watch.status == "CANCELLED"
        assert second_watch.status == "EXPIRED"
        assert third_watch.status == "ERROR"
        assert first_watch.resolved_at is not None
        assert second_watch.resolved_at is not None
        assert third_watch.resolved_at is not None
