
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    TelegramMessage,
    TelegramMedia,
    TelegramMessageEdit,
)
from app.ai import message_aggregator as aggregator


CHAT_ID = -1003014486607
OTHER_CHAT_ID = -1009999999999

T0 = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def sqlite_session_factory(monkeypatch):
    """SQLite sementara, tanpa koneksi ke Supabase."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)

    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    monkeypatch.setattr(
        aggregator,
        "SessionLocal",
        factory,
    )

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


# ============================================================
# DATABASE HELPERS
# ============================================================

def add_message(
    factory,
    telegram_message_id,
    timestamp,
    chat_id=CHAT_ID,
    text="Test message",
    reply_to_id=None,
    include_telegram_date=True,
):
    """Menyimpan pesan Telegram simulasi."""

    with factory() as db:
        message = TelegramMessage(
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            sender="mentor-test",
            message_text=text,
            reply_to_id=reply_to_id,
            is_edited=False,
            received_at=timestamp,
            telegram_date=(
                timestamp if include_telegram_date else None
            ),
        )

        db.add(message)
        db.commit()
        db.refresh(message)

        return message.id


# ============================================================
# MESSAGE COLLECTION TESTS
# ============================================================

def test_window_waktu_dan_pemisahan_chat(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    add_message(factory, 99, T0 - timedelta(minutes=5))
    add_message(factory, 100, T0, text="Pantau sell")
    add_message(factory, 101, T0 + timedelta(minutes=4))
    add_message(factory, 102, T0 + timedelta(minutes=11))

    add_message(
        factory,
        200,
        T0 + timedelta(minutes=1),
        chat_id=OTHER_CHAT_ID,
    )

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
    )

    ids = [
        item["telegram_message_id"]
        for item in result["messages"]
    ]

    assert ids == [99, 100, 101]
    assert result["messages"][1]["reason"] == "target"


def test_reply_parent_di_luar_window_dimasukkan(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    add_message(
        factory,
        900,
        T0 - timedelta(minutes=30),
        text="90 pips",
    )

    add_message(
        factory,
        100,
        T0,
        text="Tahan posisi",
        reply_to_id=900,
    )

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
    )

    messages = {
        item["telegram_message_id"]: item
        for item in result["messages"]
    }

    assert 100 in messages
    assert 900 in messages
    assert messages[900]["reason"] == "reply_parent"
    assert messages[900]["text"] == "90 pips"


def test_media_diabaikan_dan_edit_history_dikumpulkan(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    database_id = add_message(
        factory,
        100,
        T0,
        text="Entry gold",
    )

    with factory() as db:
        db.add(
            TelegramMedia(
                message_id=database_id,
                media_type="photo",
                file_path="data/media/test.jpg",
                mime_type="image/jpeg",
                file_size=1024,
            )
        )

        db.add_all([
            TelegramMessageEdit(
                message_id=database_id,
                old_text="Entry",
                new_text="Entry gold",
                edited_at=T0 + timedelta(minutes=1),
            ),
            TelegramMessageEdit(
                message_id=database_id,
                old_text="Entry gold",
                new_text="Entry gold sekarang",
                edited_at=T0 + timedelta(minutes=2),
            ),
        ])

        db.commit()

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
    )

    message = result["messages"][0]

    assert "media" not in message
    assert len(message["edit_history"]) == 2

    assert message["edit_history"][0]["old_text"] == "Entry"
    assert message["edit_history"][1]["new_text"] == (
        "Entry gold sekarang"
    )


def test_received_at_sebagai_fallback_waktu(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    add_message(
        factory,
        100,
        T0,
        text="Target tanpa tanggal Telegram",
        include_telegram_date=False,
    )

    add_message(
        factory,
        101,
        T0 + timedelta(minutes=3),
        include_telegram_date=False,
    )

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
    )

    assert len(result["messages"]) == 2

    target = result["messages"][0]

    assert target["telegram_message_id"] == 100
    assert target["telegram_date"] is None
    assert target["received_at"].startswith(
        "2026-11-03T05:00"
    )


def test_max_messages_memilih_pesan_terdekat(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    add_message(factory, 100, T0)

    add_message(
        factory,
        101,
        T0 + timedelta(minutes=1),
    )

    add_message(
        factory,
        102,
        T0 - timedelta(minutes=1),
    )

    add_message(
        factory,
        103,
        T0 + timedelta(minutes=2),
    )

    add_message(
        factory,
        104,
        T0 - timedelta(minutes=2),
    )

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
        window_minutes=10,
        max_messages=3,
    )

    ids = {
        item["telegram_message_id"]
        for item in result["messages"]
    }

    assert ids == {100, 101, 102}
    assert len(result["messages"]) == 3


def test_target_tidak_ditemukan(
    sqlite_session_factory,
):
    result = aggregator.collect_message_bundle(
        CHAT_ID,
        999999,
    )

    assert result is None


def test_target_selalu_masuk_bundle(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    add_message(
        factory,
        100,
        T0,
        text="Target utama",
    )

    for i in range(101, 111):
        add_message(
            factory,
            i,
            T0 + timedelta(minutes=1),
        )

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
        max_messages=2,
    )

    ids = [
        item["telegram_message_id"]
        for item in result["messages"]
    ]

    assert 100 in ids
    assert len(ids) == 2


def test_media_tanpa_teks_diabaikan(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    database_id = add_message(
        factory,
        100,
        T0,
        text="",
    )

    with factory() as db:
        db.add(
            TelegramMedia(
                message_id=database_id,
                media_type="video",
                file_path="data/media/test.mp4",
                mime_type="video/mp4",
                file_size=1024,
            )
        )
        db.commit()

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
    )

    assert result is None


def test_pesan_kosong_tidak_masuk_context(
    sqlite_session_factory,
):
    factory = sqlite_session_factory

    add_message(
        factory,
        100,
        T0,
        text="Pantau sell",
    )

    add_message(
        factory,
        101,
        T0 + timedelta(minutes=1),
        text="   ",
    )

    result = aggregator.collect_message_bundle(
        CHAT_ID,
        100,
    )

    ids = [
        item["telegram_message_id"]
        for item in result["messages"]
    ]

    assert ids == [100]


# ============================================================
# CLASSIFICATION TESTS
# ============================================================

def test_classify_new_signal_and_stop_loss():
    roles = aggregator.classify_message(
        "3896-3894 buy zone\nSl20/30 pips"
    )

    assert aggregator.ROLE_NEW_SIGNAL in roles
    assert aggregator.ROLE_STOP_LOSS in roles


def test_classify_entry_confirmation():
    roles = aggregator.classify_message(
        "Yuk sell again"
    )

    assert aggregator.ROLE_ENTRY_REQUEST in roles
    assert aggregator.ROLE_NEW_SIGNAL not in roles


def test_classify_try_sell_waits_for_zone():
    roles = aggregator.classify_message(
        "Try sell"
    )

    assert aggregator.ROLE_ENTRY_REQUEST in roles
    assert aggregator.ROLE_NEW_SIGNAL not in roles


def test_classify_try_buy_waits_for_zone():
    roles = aggregator.classify_message(
        "Try buy"
    )

    assert aggregator.ROLE_ENTRY_REQUEST in roles
    assert aggregator.ROLE_NEW_SIGNAL not in roles


def test_classify_partial_tp():
    roles = aggregator.classify_message(
        "Tp 1 selesai 60 pips"
    )

    assert aggregator.ROLE_PARTIAL_TP in roles
    assert aggregator.ROLE_POSITION_UPDATE in roles


def test_classify_hit_sl_information_only():
    roles = aggregator.classify_message(
        "Hit sl"
    )

    assert aggregator.ROLE_STOP_LOSS in roles
    assert aggregator.ROLE_POSITION_CLOSE not in roles


def test_classify_explicit_reentry_plan():
    roles = aggregator.classify_message(
        "Cari re-entry sell"
    )

    assert aggregator.ROLE_REENTRY_PLAN in roles
    assert aggregator.ROLE_REENTRY not in roles


def test_classify_future_reentry_plan():
    roles = aggregator.classify_message(
        "Ntar ku carikan re entry nambah layer"
    )

    assert aggregator.ROLE_REENTRY_PLAN in roles
    assert aggregator.ROLE_NEW_SIGNAL not in roles


def test_classify_bare_price_zone():
    roles = aggregator.classify_message(
        "4050-4052"
    )

    assert aggregator.ROLE_PRICE_ZONE in roles
    assert aggregator.ROLE_NEW_SIGNAL not in roles


def test_classify_skip():
    roles = aggregator.classify_message(
        "Skip"
    )

    assert aggregator.ROLE_SKIP in roles


def test_classify_cisd_condition():
    roles = aggregator.classify_message(
        "Tunggu CISD M1"
    )

    assert aggregator.ROLE_ENTRY_CONDITION in roles


# ============================================================
# EPISODE GROUPING TESTS
# ============================================================

def test_episode_grouping_sl_tp_are_information_only():
    messages = [
        {"text": "4046-4048 sell zone"},
        {"text": "Sl20/30 pips"},
        {"text": "Tp 1 selesai 60 pips"},
        {"text": "Hit sl"},
        {"text": "Selamat pagi king"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] == "trade_1"
    assert messages[1]["trade_episode_id"] == "trade_1"
    assert messages[2]["trade_episode_id"] == "trade_1"

    # SL bukan bukti bahwa episode harus ditutup.
    assert messages[3]["trade_episode_id"] == "trade_1"
    assert messages[3]["trade_episode_status"] == "OPEN"

    assert messages[4]["trade_episode_id"] is None


def test_try_sell_waits_for_zone():
    messages = [
        {"text": "Try sell"},
        {"text": "Stndby"},
        {"text": "4050-4052"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] is None
    assert messages[0]["trade_episode_status"] == "WAITING_FOR_ZONE"
    assert messages[0]["pending_direction"] == "SELL"

    assert messages[2]["trade_episode_id"] == "trade_1"
    assert messages[2]["trade_episode_status"] == "OPEN"
    assert messages[2]["signal_direction"] == "SELL"
    assert messages[2]["entry_zone"]["midpoint"] == 4051


def test_try_buy_waits_for_zone():
    messages = [
        {"text": "Try buy"},
        {"text": "4110-4112"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_status"] == "WAITING_FOR_ZONE"

    assert messages[1]["trade_episode_id"] == "trade_1"
    assert messages[1]["signal_direction"] == "BUY"
    assert messages[1]["entry_zone"]["midpoint"] == 4111


def test_reentry_plan_does_not_start_episode():
    messages = [
        {"text": "4046-4048 sell zone"},
        {"text": "Hit sl"},
        {"text": "Ntar ku carikan re entry nambah layer"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] == "trade_1"

    assert (
        aggregator.ROLE_REENTRY_PLAN
        in messages[2]["message_roles"]
    )

    assert messages[2]["trade_episode_id"] is None


def test_reentry_plan_without_existing_episode():
    messages = [
        {"text": "Ntar ku carikan re entry"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] is None
    assert messages[0]["trade_episode_status"] is None


def test_skip_marks_episode_without_closing_mt5_position():
    messages = [
        {"text": "4046-4048 sell zone"},
        {"text": "Skip"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[1]["trade_episode_id"] == "trade_1"
    assert messages[1]["trade_episode_status"] == "SKIP_REQUESTED"


def test_skip_cancels_waiting_entry_request():
    messages = [
        {"text": "Try sell"},
        {"text": "Skip"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_status"] == "WAITING_FOR_ZONE"
    assert messages[1]["trade_episode_id"] is None
    assert messages[1]["trade_episode_status"] is None


def test_bare_zone_without_pending_direction_is_not_signal():
    messages = [
        {"text": "4050-4052"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] is None
    assert aggregator.ROLE_PRICE_ZONE in messages[0]["message_roles"]


def test_new_signal_starts_new_episode():
    messages = [
        {"text": "4046-4048 sell zone"},
        {"text": "4050-4052 buy zone"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] == "trade_1"
    assert messages[1]["trade_episode_id"] == "trade_2"


def test_hit_sl_does_not_start_new_episode():
    messages = [
        {"text": "4046-4048 sell zone"},
        {"text": "Hit sl"},
        {"text": "Selamat pagi"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] == "trade_1"
    assert messages[1]["trade_episode_id"] == "trade_1"
    assert messages[1]["trade_episode_status"] == "OPEN"
    assert messages[2]["trade_episode_id"] is None


def test_sell_again_waits_for_following_zone():
    messages = [
        {"text": "4046-4048 sell zone"},
        {"text": "Yuk sell again"},
        {"text": "4050-4052"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_id"] == "trade_1"

    assert messages[1]["trade_episode_status"] == "WAITING_FOR_ZONE"
    assert messages[1]["pending_direction"] == "SELL"

    assert messages[2]["trade_episode_id"] == "trade_2"
    assert messages[2]["signal_direction"] == "SELL"
    assert messages[2]["entry_zone"]["midpoint"] == 4051


def test_skip_then_new_signal():
    messages = [
        {"text": "Try sell"},
        {"text": "Skip"},
        {"text": "4050-4052 sell zone"},
    ]

    aggregator.classify_and_group_messages(messages)

    assert messages[0]["trade_episode_status"] == "WAITING_FOR_ZONE"
    assert messages[1]["trade_episode_id"] is None
    assert messages[2]["trade_episode_id"] == "trade_1"
