from datetime import datetime, timezone

import pytest

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    TelegramMessage,
    TradeEpisode,
    TradeEpisodeMessage,
)

from app.signals import trade_episode_repository as repository


CHAT_ID = -1003014486607
OTHER_CHAT_ID = -1009999999999


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def episode_db():
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


# ============================================================
# HELPERS
# ============================================================

def save_message(
    factory,
    telegram_message_id,
    chat_id=CHAT_ID,
    text="BUY XAUUSD 4100-4102",
):
    with factory() as db:
        message = TelegramMessage(
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            sender="mentor-test",
            message_text=text,
            received_at=datetime.now(timezone.utc),
            telegram_date=datetime.now(timezone.utc),
            is_edited=False,
        )

        db.add(message)
        db.commit()
        db.refresh(message)

        return message.id


def create_episode(
    factory,
    telegram_message_id,
    chat_id=CHAT_ID,
    direction="BUY",
    entry_low="4100",
    entry_high="4102",
):
    save_message(
        factory,
        telegram_message_id,
        chat_id=chat_id,
    )

    with factory() as db:
        episode = repository.create_or_get_trade_episode(
            db,
            chat_id=chat_id,
            anchor_telegram_message_id=telegram_message_id,
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
        )

        db.commit()

        return episode.id


def count_rows(db, model):
    return db.scalar(
        select(func.count()).select_from(model)
    )


# ============================================================
# CREATE EPISODE TESTS
# ============================================================

def test_create_episode_dengan_anchor(
    episode_db,
):
    factory = episode_db

    anchor_db_id = save_message(
        factory,
        telegram_message_id=9001,
    )

    with factory() as db:
        episode = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9001,
            direction="BUY",
            entry_low="4100",
            entry_high="4102",
        )

        db.commit()

        assert episode.episode_key == (
            f"trade_{CHAT_ID}_9001"
        )

        assert episode.chat_id == CHAT_ID
        assert episode.anchor_message_id == anchor_db_id
        assert episode.direction == "BUY"

        assert str(episode.entry_low) == "4100.00000"
        assert str(episode.entry_high) == "4102.00000"
        assert str(episode.entry_midpoint) == "4101.00000"

        assert episode.status == "OPEN"

        mappings = repository.list_trade_episode_messages(
            db,
            episode.id,
        )

        assert len(mappings) == 1

        assert mappings[0].telegram_message_db_id == anchor_db_id
        assert mappings[0].message_role == "NEW_SIGNAL"
        assert mappings[0].relation_type == "ANCHOR"
        assert mappings[0].association_method == "ANCHOR"


def test_create_episode_idempotent(
    episode_db,
):
    factory = episode_db

    save_message(factory, 9002)

    with factory() as db:
        first = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9002,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        db.commit()

        first_id = first.id

    with factory() as db:
        second = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9002,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        db.commit()

        assert second.id == first_id

        assert count_rows(db, TradeEpisode) == 1
        assert count_rows(db, TradeEpisodeMessage) == 1


def test_episode_key_berbeda_untuk_chat_berbeda(
    episode_db,
):
    factory = episode_db

    save_message(
        factory,
        telegram_message_id=9003,
        chat_id=CHAT_ID,
    )

    save_message(
        factory,
        telegram_message_id=9003,
        chat_id=OTHER_CHAT_ID,
    )

    with factory() as db:
        first = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9003,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        second = repository.create_or_get_trade_episode(
            db,
            chat_id=OTHER_CHAT_ID,
            anchor_telegram_message_id=9003,
            direction="SELL",
            entry_low=4200,
            entry_high=4202,
        )

        db.commit()

        assert first.episode_key == (
            f"trade_{CHAT_ID}_9003"
        )

        assert second.episode_key == (
            f"trade_{OTHER_CHAT_ID}_9003"
        )

        assert first.id != second.id


def test_episode_conflict_jika_setup_berubah(
    episode_db,
):
    factory = episode_db

    save_message(factory, 9004)

    with factory() as db:
        repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9004,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        db.commit()

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeConflictError
        ):
            repository.create_or_get_trade_episode(
                db,
                chat_id=CHAT_ID,
                anchor_telegram_message_id=9004,
                direction="BUY",
                entry_low=4101,
                entry_high=4103,
            )


def test_episode_conflict_jika_direction_berubah(
    episode_db,
):
    factory = episode_db

    save_message(factory, 9005)

    with factory() as db:
        repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9005,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        db.commit()

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeConflictError
        ):
            repository.create_or_get_trade_episode(
                db,
                chat_id=CHAT_ID,
                anchor_telegram_message_id=9005,
                direction="SELL",
                entry_low=4100,
                entry_high=4102,
            )


# ============================================================
# MESSAGE LINKING TESTS
# ============================================================

def test_link_message_ke_episode(
    episode_db,
):
    factory = episode_db

    episode_id = create_episode(
        factory,
        telegram_message_id=9100,
    )

    message_db_id = save_message(
        factory,
        telegram_message_id=9101,
        text="CISD M1 confirmed",
    )

    with factory() as db:
        mapping = repository.link_trade_episode_message(
            db,
            episode_id=episode_id,
            telegram_message_db_id=message_db_id,
            message_role="ENTRY_CONDITION",
            relation_type="ENTRY_CONFIRMATION",
            association_method="SAME_WINDOW",
        )

        db.commit()

        assert mapping.episode_id == episode_id
        assert mapping.telegram_message_db_id == message_db_id
        assert mapping.message_role == "ENTRY_CONDITION"
        assert mapping.relation_type == "ENTRY_CONFIRMATION"
        assert mapping.association_method == "SAME_WINDOW"


def test_link_message_idempotent(
    episode_db,
):
    factory = episode_db

    episode_id = create_episode(
        factory,
        telegram_message_id=9102,
    )

    message_db_id = save_message(
        factory,
        telegram_message_id=9103,
        text="CISD confirmed",
    )

    with factory() as db:
        first = repository.link_trade_episode_message(
            db,
            episode_id=episode_id,
            telegram_message_db_id=message_db_id,
            message_role="ENTRY_CONDITION",
            relation_type="ENTRY_CONFIRMATION",
            association_method="SAME_WINDOW",
        )

        db.commit()

        first_id = first.id

    with factory() as db:
        second = repository.link_trade_episode_message(
            db,
            episode_id=episode_id,
            telegram_message_db_id=message_db_id,
            message_role="POSITION_UPDATE",
            relation_type="POSITION_UPDATE",
            association_method="PERSISTENT_LOOKUP",
        )

        db.commit()

        assert second.id == first_id

        assert second.message_role == "POSITION_UPDATE"
        assert second.relation_type == "POSITION_UPDATE"
        assert second.association_method == "PERSISTENT_LOOKUP"

        assert count_rows(db, TradeEpisodeMessage) == 2


def test_satu_pesan_tidak_bisa_masuk_dua_episode(
    episode_db,
):
    factory = episode_db

    first_episode_id = create_episode(
        factory,
        telegram_message_id=9200,
    )

    second_episode_id = create_episode(
        factory,
        telegram_message_id=9201,
    )

    shared_message_id = save_message(
        factory,
        telegram_message_id=9202,
        text="CISD confirmed",
    )

    with factory() as db:
        repository.link_trade_episode_message(
            db,
            episode_id=first_episode_id,
            telegram_message_db_id=shared_message_id,
            message_role="ENTRY_CONDITION",
            relation_type="ENTRY_CONFIRMATION",
            association_method="SAME_WINDOW",
        )

        db.commit()

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeConflictError
        ):
            repository.link_trade_episode_message(
                db,
                episode_id=second_episode_id,
                telegram_message_db_id=shared_message_id,
                message_role="ENTRY_CONDITION",
                relation_type="ENTRY_CONFIRMATION",
                association_method="SAME_WINDOW",
            )

    with factory() as db:
        owner = repository.get_trade_episode_for_message(
            db,
            shared_message_id,
        )

        assert owner.id == first_episode_id


def test_pesan_dari_chat_lain_ditolak(
    episode_db,
):
    factory = episode_db

    episode_id = create_episode(
        factory,
        telegram_message_id=9300,
        chat_id=CHAT_ID,
    )

    other_message_id = save_message(
        factory,
        telegram_message_id=9301,
        chat_id=OTHER_CHAT_ID,
    )

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeConflictError
        ):
            repository.link_trade_episode_message(
                db,
                episode_id=episode_id,
                telegram_message_db_id=other_message_id,
                message_role="COMMENTARY",
                relation_type="CONTEXT",
                association_method="SAME_WINDOW",
            )


def test_pesan_anchor_tidak_bisa_diubah_menjadi_context(
    episode_db,
):
    factory = episode_db

    anchor_db_id = save_message(
        factory,
        telegram_message_id=9400,
    )

    with factory() as db:
        episode = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9400,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        db.commit()

        episode_id = episode.id

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeConflictError
        ):
            repository.link_trade_episode_message(
                db,
                episode_id=episode_id,
                telegram_message_db_id=anchor_db_id,
                message_role="COMMENTARY",
                relation_type="CONTEXT",
                association_method="SAME_WINDOW",
            )


# ============================================================
# VALIDATION TESTS
# ============================================================

def test_direction_invalid_ditolak(
    episode_db,
):
    factory = episode_db

    save_message(factory, 9500)

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeValidationError
        ):
            repository.create_or_get_trade_episode(
                db,
                chat_id=CHAT_ID,
                anchor_telegram_message_id=9500,
                direction="HOLD",
                entry_low=4100,
                entry_high=4102,
            )


def test_entry_zone_invalid_ditolak(
    episode_db,
):
    factory = episode_db

    save_message(factory, 9501)

    with factory() as db:
        with pytest.raises(
            repository.TradeEpisodeValidationError
        ):
            repository.create_or_get_trade_episode(
                db,
                chat_id=CHAT_ID,
                anchor_telegram_message_id=9501,
                direction="BUY",
                entry_low=4102,
                entry_high=4100,
            )


def test_anchor_tidak_ditemukan(
    episode_db,
):
    with episode_db() as db:
        with pytest.raises(
            repository.TradeEpisodeNotFoundError
        ):
            repository.create_or_get_trade_episode(
                db,
                chat_id=CHAT_ID,
                anchor_telegram_message_id=999999,
                direction="BUY",
                entry_low=4100,
                entry_high=4102,
            )

def test_persist_bundle_menghubungkan_try_sell_zone_dan_cisd(
    episode_db,
):
    factory = episode_db

    save_message(
        factory,
        9600,
        text="Try sell",
    )

    save_message(
        factory,
        9601,
        text="4100-4102",
    )

    cisd_db_id = save_message(
        factory,
        9602,
        text="CISD M1 confirmed",
    )

    episode_key = f"trade_{CHAT_ID}_9601"

    bundle = {
        "chat_id": CHAT_ID,
        "messages": [
            {
                "telegram_message_id": 9600,
                "text": "Try sell",
                "message_roles": ["ENTRY_REQUEST"],
                "pending_direction": "SELL",
                "trade_episode_id": None,
            },
            {
                "telegram_message_id": 9601,
                "text": "4100-4102",
                "message_roles": [
                    "PRICE_ZONE",
                    "NEW_SIGNAL",
                ],
                "signal_direction": "SELL",
                "entry_zone": {
                    "low": 4100,
                    "high": 4102,
                    "midpoint": 4101,
                },
                "trade_episode_id": episode_key,
            },
            {
                "telegram_message_id": 9602,
                "text": "CISD M1 confirmed",
                "message_roles": ["ENTRY_CONDITION"],
                "trade_episode_id": episode_key,
            },
        ],
    }

    with factory() as db:
        result = repository.persist_bundle_trade_episodes(
            db,
            bundle,
        )

        db.commit()

        episode = repository.get_trade_episode_by_key(
            db,
            episode_key,
        )

        assert episode is not None
        assert episode.direction == "SELL"

        mappings = repository.list_trade_episode_messages(
            db,
            episode.id,
        )

        assert len(mappings) == 3

        owners = {
            item.telegram_message_db_id
            for item in mappings
        }

        request_db_id = repository._get_telegram_message(
            db,
            CHAT_ID,
            9600,
        ).id

        anchor_db_id = repository._get_telegram_message(
            db,
            CHAT_ID,
            9601,
        ).id

        assert request_db_id in owners
        assert anchor_db_id in owners
        assert cisd_db_id in owners

        assert result["created"] == 1

        assert bundle["messages"][0]["trade_episode_id"] == (
            episode_key
        )

        assert bundle["messages"][2]["trade_episode_id"] == (
            episode_key
        )



def test_persist_bundle_reply_to_lama_dapat_menghubungkan_skip(
    episode_db,
):
    factory = episode_db

    # create_episode sudah membuat pesan anchor 9700.
    episode_id = create_episode(
        factory,
        telegram_message_id=9700,
    )

    # Ambil ID database dari pesan yang sudah dibuat.
    # Jangan membuat pesan Telegram 9700 untuk kedua kalinya.
    with factory() as db:
        anchor = repository._get_telegram_message(
            db,
            CHAT_ID,
            9700,
        )

        assert anchor is not None
        anchor_db_id = anchor.id

    # Buat pesan balasan yang merujuk anchor.
    with factory() as db:
        reply = TelegramMessage(
            chat_id=CHAT_ID,
            telegram_message_id=9701,
            sender="mentor-test",
            message_text="Skip setup ini",
            reply_to_id=9700,
            is_edited=False,
            received_at=datetime.now(timezone.utc),
            telegram_date=datetime.now(timezone.utc),
        )

        db.add(reply)
        db.commit()
        db.refresh(reply)

        reply_db_id = reply.id

    bundle = {
        "chat_id": CHAT_ID,
        "messages": [
            {
                "telegram_message_id": 9701,
                "reply_to_id": 9700,
                "text": "Skip setup ini",
                "message_roles": ["SKIP"],
                "trade_episode_id": None,
            }
        ],
    }

    with factory() as db:
        repository.persist_bundle_trade_episodes(
            db,
            bundle,
        )

        db.commit()

        owner = repository.get_trade_episode_for_message(
            db,
            reply_db_id,
        )

        episode = db.get(
            TradeEpisode,
            episode_id,
        )

        assert owner is not None
        assert owner.id == episode_id

        assert episode.status == "SKIP_REQUESTED"

        mapping = repository._get_message_mapping(
            db,
            reply_db_id,
        )

        assert mapping.relation_type == "SKIP_REQUEST"
        assert mapping.association_method == "REPLY_TO"

        assert anchor_db_id == episode.anchor_message_id


    with factory() as db:
        repository.persist_bundle_trade_episodes(
            db,
            bundle,
        )

        db.commit()

        owner = repository.get_trade_episode_for_message(
            db,
            reply_db_id,
        )

        episode = db.get(
            TradeEpisode,
            episode_id,
        )

        assert owner.id == episode_id
        assert episode.status == "SKIP_REQUESTED"

        mapping = repository._get_message_mapping(
            db,
            reply_db_id,
        )

        assert mapping.relation_type == "SKIP_REQUEST"
        assert mapping.association_method == "REPLY_TO"

        assert anchor_db_id == episode.anchor_message_id


def test_persist_bundle_tidak_menekan_episode_tanpa_bukti(
    episode_db,
):
    factory = episode_db

    create_episode(
        factory,
        telegram_message_id=9800,
    )

    create_episode(
        factory,
        telegram_message_id=9801,
    )

    message_db_id = save_message(
        factory,
        telegram_message_id=9802,
        text="CISD confirmed",
    )

    bundle = {
        "chat_id": CHAT_ID,
        "messages": [
            {
                "telegram_message_id": 9802,
                "text": "CISD confirmed",
                "message_roles": ["ENTRY_CONDITION"],
                "trade_episode_id": None,
                "reply_to_id": None,
            }
        ],
    }

    with factory() as db:
        result = repository.persist_bundle_trade_episodes(
            db,
            bundle,
        )

        db.commit()

        mapping = repository._get_message_mapping(
            db,
            message_db_id,
        )

        assert mapping is None
        assert result["unresolved"] == 1
        assert count_rows(db, TradeEpisode) == 2
