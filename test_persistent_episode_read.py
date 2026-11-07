from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    TelegramMessage,
    TradeEpisodeMessage,
)
from app.ai import message_aggregator as aggregator
from app.signals import trade_episode_repository as repository


CHAT_ID = -1003014486607
T0 = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def episode_read_db(monkeypatch):
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

    monkeypatch.setattr(aggregator, "SessionLocal", factory)

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


def add_message(
    factory,
    telegram_message_id,
    timestamp,
    text,
    reply_to_id=None,
):
    with factory() as db:
        message = TelegramMessage(
            chat_id=CHAT_ID,
            telegram_message_id=telegram_message_id,
            sender="mentor-test",
            message_text=text,
            reply_to_id=reply_to_id,
            is_edited=False,
            received_at=timestamp,
            telegram_date=timestamp,
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        return message.id


def test_bundle_reads_old_anchor_through_persistent_mapping(
    episode_read_db,
):
    factory = episode_read_db

    add_message(
        factory,
        9900,
        T0,
        "BUY XAUUSD 4100-4102",
    )

    target_db_id = add_message(
        factory,
        9901,
        T0 + timedelta(minutes=60),
        "CISD M1 confirmed",
    )

    with factory() as db:
        episode = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9900,
            direction="BUY",
            entry_low=4100,
            entry_high=4102,
        )

        repository.link_trade_episode_message(
            db,
            episode_id=episode.id,
            telegram_message_db_id=target_db_id,
            message_role="ENTRY_CONDITION",
            relation_type="ENTRY_CONFIRMATION",
            association_method="MANUAL",
        )

        episode_key = episode.episode_key
        db.commit()

    with factory() as db:
        result = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=9901,
            db=db,
        )

    assert result is not None

    anchor = next(
        item for item in result["messages"]
        if item["telegram_message_id"] == 9900
    )
    target = next(
        item for item in result["messages"]
        if item["telegram_message_id"] == 9901
    )

    assert anchor["reason"] == "persistent_episode"
    assert anchor["trade_episode_id"] == episode_key
    assert target["trade_episode_id"] == episode_key
    assert anchor["trade_episode_status"] == "OPEN"
    assert target["trade_episode_status"] == "OPEN"
    assert "_database_id" not in anchor
    assert "_database_id" not in target


def test_reply_target_reads_episode_without_writing_mapping(
    episode_read_db,
):
    factory = episode_read_db

    anchor_db_id = add_message(
        factory,
        9910,
        T0,
        "SELL XAUUSD 4110-4112",
    )

    target_db_id = add_message(
        factory,
        9911,
        T0 + timedelta(minutes=60),
        "Skip setup ini",
        reply_to_id=9910,
    )

    with factory() as db:
        episode = repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9910,
            direction="SELL",
            entry_low=4110,
            entry_high=4112,
        )
        episode_key = episode.episode_key
        episode_id = episode.id
        db.commit()

    with factory() as db:
        result = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=9911,
            db=db,
        )

        # Read path tidak membuat mapping baru.
        target_mapping = db.scalar(
            select(TradeEpisodeMessage).where(
                TradeEpisodeMessage.telegram_message_db_id
                == target_db_id
            )
        )
        assert target_mapping is None

    assert result is not None

    target = next(
        item for item in result["messages"]
        if item["telegram_message_id"] == 9911
    )
    anchor = next(
        item for item in result["messages"]
        if item["telegram_message_id"] == 9910
    )

    assert target["trade_episode_id"] == episode_key
    assert target["episode_association_method"] == "REPLY_TO"
    assert anchor["trade_episode_id"] == episode_key
    assert anchor_db_id is not None
    assert episode_id is not None


def test_message_without_mapping_or_reply_does_not_inherit_open_episode(
    episode_read_db,
):
    factory = episode_read_db

    add_message(
        factory,
        9920,
        T0,
        "BUY XAUUSD 4120-4122",
    )

    add_message(
        factory,
        9921,
        T0 + timedelta(minutes=60),
        "CISD M1 confirmed",
    )

    with factory() as db:
        repository.create_or_get_trade_episode(
            db,
            chat_id=CHAT_ID,
            anchor_telegram_message_id=9920,
            direction="BUY",
            entry_low=4120,
            entry_high=4122,
        )
        db.commit()

    with factory() as db:
        result = aggregator.collect_message_bundle(
            chat_id=CHAT_ID,
            telegram_message_id=9921,
            db=db,
        )

    assert result is not None
    assert len(result["messages"]) == 1

    target = result["messages"][0]
    assert target["telegram_message_id"] == 9921
    assert target["trade_episode_id"] is None
