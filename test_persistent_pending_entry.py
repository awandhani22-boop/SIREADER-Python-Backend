from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    TelegramMessage,
    TradeEpisode,
    TradeEpisodeMessage,
    TradePendingEntry,
)
from app.ai import message_aggregator as aggregator
from app.signals import trade_episode_repository as repository


CHAT_ID = -1003014486607
T0 = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def pending_db():
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
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def add_message(factory, msg_id, text, at, reply_to_id=None):
    with factory() as db:
        row = TelegramMessage(
            chat_id=CHAT_ID,
            telegram_message_id=msg_id,
            sender="mentor-test",
            message_text=text,
            reply_to_id=reply_to_id,
            is_edited=False,
            received_at=at,
            telegram_date=at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def process_bundle(db, chat_id, messages):
    aggregator.classify_and_group_messages(
        messages,
        chat_id=chat_id,
    )
    bundle = {
        "chat_id": chat_id,
        "messages": messages,
    }
    result = repository.persist_bundle_trade_episodes(db, bundle)
    db.commit()
    return bundle, result


def get_pending(db, message_db_id):
    return db.scalar(
        select(TradePendingEntry).where(
            TradePendingEntry.source_message_id == message_db_id
        )
    )


def test_try_sell_terdaftar_sebagai_pending_persisten(pending_db):
    factory = pending_db
    request_db_id = add_message(
        factory,
        10001,
        "Try sell",
        T0,
    )

    with factory() as db:
        bundle, result = process_bundle(
            db,
            CHAT_ID,
            [{
                "telegram_message_id": 10001,
                "text": "Try sell",
                "reply_to_id": None,
                "edit_history": [],
            }],
        )

        pending = get_pending(db, request_db_id)
        assert pending is not None
        assert pending.direction == "SELL"
        assert pending.status == "WAITING_FOR_ZONE"
        assert pending.expires_at - pending.requested_at == timedelta(minutes=30)
        assert result["created"] == 0
        assert db.scalar(select(TradeEpisode)) is None

    # Pemrosesan ulang harus idempoten.
    with factory() as db:
        process_bundle(
            db,
            CHAT_ID,
            [{
                "telegram_message_id": 10001,
                "text": "Try sell",
                "reply_to_id": None,
                "edit_history": [],
            }],
        )
        rows = db.scalars(select(TradePendingEntry)).all()
        assert len(rows) == 1
        assert rows[0].status == "WAITING_FOR_ZONE"


def test_try_sell_dipasangkan_dengan_zona_dua_puluh_menit_kemudian(
    pending_db,
):
    factory = pending_db
    request_db_id = add_message(
        factory,
        10101,
        "Try sell",
        T0,
    )

    # Pipeline pertama menyimpan pending request.
    with factory() as db:
        process_bundle(
            db,
            CHAT_ID,
            [{
                "telegram_message_id": 10101,
                "text": "Try sell",
                "reply_to_id": None,
                "edit_history": [],
            }],
        )

    zone_db_id = add_message(
        factory,
        10102,
        "4100-4102",
        T0 + timedelta(minutes=20),
    )

    # Bundle zona hanya memakai jendela 10 menit.
    # Request 20 menit sebelumnya harus ditambahkan dari DB pending.
    with factory() as db:
        bundle = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=10102,
            window_minutes=10,
            db=db,
        )

        assert bundle is not None
        request_item = next(
            item for item in bundle["messages"]
            if item["telegram_message_id"] == 10101
        )
        zone_item = next(
            item for item in bundle["messages"]
            if item["telegram_message_id"] == 10102
        )

        assert request_item["reason"] == "persistent_pending_entry"
        assert request_item["message_roles"] == ["ENTRY_REQUEST"]
        assert zone_item["signal_direction"] == "SELL"
        assert "NEW_SIGNAL" in zone_item["message_roles"]
        assert zone_item["trade_episode_id"] == f"trade_{CHAT_ID}_10102"

        sync_result = repository.persist_bundle_trade_episodes(
            db,
            bundle,
        )
        db.commit()

        episode = db.scalar(
            select(TradeEpisode).where(
                TradeEpisode.episode_key == f"trade_{CHAT_ID}_10102"
            )
        )
        assert episode is not None
        assert episode.direction == "SELL"
        assert sync_result["created"] == 1

        pending = get_pending(db, request_db_id)
        assert pending.status == "MATCHED"
        assert pending.matched_episode_id == episode.id

        request_mapping = db.scalar(
            select(TradeEpisodeMessage).where(
                TradeEpisodeMessage.telegram_message_db_id == request_db_id
            )
        )
        assert request_mapping is not None
        assert request_mapping.episode_id == episode.id
        assert request_mapping.relation_type == "ENTRY_REQUEST"
        assert request_mapping.association_method == "PERSISTENT_PENDING"

        assert zone_db_id == episode.anchor_message_id


def test_dua_pending_sell_membuat_kandidat_ambigu(pending_db):
    factory = pending_db
    first_db_id = add_message(factory, 10201, "Try sell", T0)
    second_db_id = add_message(
        factory,
        10202,
        "Try sell",
        T0 + timedelta(minutes=5),
    )

    with factory() as db:
        process_bundle(
            db,
            CHAT_ID,
            [
                {"telegram_message_id": 10201, "text": "Try sell", "edit_history": []},
                {"telegram_message_id": 10202, "text": "Try sell", "edit_history": []},
            ],
        )

    add_message(
        factory,
        10203,
        "4100-4102",
        T0 + timedelta(minutes=20),
    )

    with factory() as db:
        bundle = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=10203,
            window_minutes=10,
            db=db,
        )
        zone = next(
            item for item in bundle["messages"]
            if item["telegram_message_id"] == 10203
        )
        assert zone.get("pending_entry_ambiguous") is True
        assert "NEW_SIGNAL" not in zone["message_roles"]

        repository.persist_bundle_trade_episodes(db, bundle)
        db.commit()

        first = get_pending(db, first_db_id)
        second = get_pending(db, second_db_id)
        assert first.status == "AMBIGUOUS"
        assert second.status == "AMBIGUOUS"
        assert db.scalar(select(TradeEpisode)) is None


def test_pending_sell_kedaluwarsa_setelah_tiga_puluh_menit(pending_db):
    factory = pending_db
    request_db_id = add_message(factory, 10301, "Try sell", T0)

    with factory() as db:
        process_bundle(
            db,
            CHAT_ID,
            [{"telegram_message_id": 10301, "text": "Try sell", "edit_history": []}],
        )

    add_message(
        factory,
        10302,
        "4100-4102",
        T0 + timedelta(minutes=31),
    )

    with factory() as db:
        bundle = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=10302,
            window_minutes=10,
            db=db,
        )
        zone = next(
            item for item in bundle["messages"]
            if item["telegram_message_id"] == 10302
        )
        assert "NEW_SIGNAL" not in zone["message_roles"]

        repository.persist_bundle_trade_episodes(db, bundle)
        db.commit()

        pending = get_pending(db, request_db_id)
        assert pending.status == "EXPIRED"
        assert pending.matched_episode_id is None
        assert db.scalar(select(TradeEpisode)) is None


def test_skip_reply_hanya_membatalkan_pending_request_yang_dituju(
    pending_db,
):
    factory = pending_db
    first_db_id = add_message(factory, 10401, "Try sell", T0)
    second_db_id = add_message(
        factory,
        10402,
        "Try buy",
        T0 + timedelta(minutes=1),
    )

    with factory() as db:
        process_bundle(
            db,
            CHAT_ID,
            [
                {"telegram_message_id": 10401, "text": "Try sell", "edit_history": []},
                {"telegram_message_id": 10402, "text": "Try buy", "edit_history": []},
            ],
        )

    add_message(
        factory,
        10403,
        "Skip setup sell ini",
        T0 + timedelta(minutes=2),
        reply_to_id=10401,
    )

    with factory() as db:
        bundle, _ = process_bundle(
            db,
            CHAT_ID,
            [{
                "telegram_message_id": 10403,
                "text": "Skip setup sell ini",
                "reply_to_id": 10401,
                "edit_history": [],
            }],
        )

        first = get_pending(db, first_db_id)
        second = get_pending(db, second_db_id)
        assert first.status == "CANCELLED"
        assert second.status == "WAITING_FOR_ZONE"
        assert db.scalar(select(TradeEpisode)) is None


def test_dua_request_dalam_bundle_sama_tidak_diikuti_pending_terakhir(
    pending_db,
):
    factory = pending_db
    add_message(factory, 10501, "Try sell", T0)
    add_message(
        factory,
        10502,
        "Try buy",
        T0 + timedelta(minutes=2),
    )
    add_message(
        factory,
        10503,
        "4100-4102",
        T0 + timedelta(minutes=5),
    )

    with factory() as db:
        bundle = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=10503,
            window_minutes=10,
            db=db,
        )
        zone = next(
            item for item in bundle["messages"]
            if item["telegram_message_id"] == 10503
        )

        assert zone.get("pending_entry_ambiguous") is True
        assert "NEW_SIGNAL" not in zone["message_roles"]

        repository.persist_bundle_trade_episodes(db, bundle)
        db.commit()

        requests = db.scalars(
            select(TradePendingEntry).order_by(TradePendingEntry.id)
        ).all()
        assert len(requests) == 2
        assert {row.status for row in requests} == {"AMBIGUOUS"}
        assert db.scalar(select(TradeEpisode)) is None
