from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    AIAnalysisLog,
    TelegramOutbox,
)
from app.approval import outbox


def make_test_db(monkeypatch):
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
        outbox,
        "SessionLocal",
        factory,
    )

    return engine, factory


def add_review(factory, status="MANUAL_REVIEW"):
    with factory() as db:
        log = AIAnalysisLog(
            chat_id=-1003014486607,
            target_message_id=123,
            status=status,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log.id


def test_review_masuk_outbox(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        add_review(factory)

        created = outbox.ensure_pending_review_notifications(
            123456789
        )

        assert created == 1

        with factory() as db:
            item = db.scalar(select(TelegramOutbox))

            assert item is not None
            assert item.status == "PENDING"
            assert item.recipient_chat_id == 123456789

    finally:
        engine.dispose()


def test_enqueue_idempotent(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        add_review(factory)

        first = outbox.ensure_pending_review_notifications(
            123456789
        )
        second = outbox.ensure_pending_review_notifications(
            123456789
        )

        assert first == 1
        assert second == 0

        with factory() as db:
            rows = db.scalars(
                select(TelegramOutbox)
            ).all()

            assert len(rows) == 1

    finally:
        engine.dispose()


def test_claim_notification(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        add_review(factory)
        outbox.ensure_pending_review_notifications(123456789)

        event = outbox.claim_next_notification()

        assert event is not None
        assert event["analysis_log_id"] == 1

        with factory() as db:
            item = db.get(TelegramOutbox, event["id"])

            assert item.status == "SENDING"
            assert item.attempts == 1

    finally:
        engine.dispose()


def test_mark_sent(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        add_review(factory)
        outbox.ensure_pending_review_notifications(123456789)

        event = outbox.claim_next_notification()

        outbox.mark_notification_sent(
            event["id"],
            telegram_message_id=987,
        )

        with factory() as db:
            item = db.get(TelegramOutbox, event["id"])

            assert item.status == "SENT"
            assert item.telegram_message_id == 987
            assert item.sent_at is not None

    finally:
        engine.dispose()


def test_mark_failed(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        add_review(factory)
        outbox.ensure_pending_review_notifications(123456789)

        event = outbox.claim_next_notification()

        outbox.mark_notification_failed(
            event["id"],
            "Simulated Telegram error",
        )

        with factory() as db:
            item = db.get(TelegramOutbox, event["id"])

            assert item.status == "FAILED"
            assert "Simulated Telegram error" in item.last_error

    finally:
        engine.dispose()


def test_mark_skipped(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        add_review(factory)
        outbox.ensure_pending_review_notifications(123456789)

        event = outbox.claim_next_notification()

        outbox.mark_notification_skipped(
            event["id"],
            "Analysis already reviewed",
        )

        with factory() as db:
            item = db.get(TelegramOutbox, event["id"])

            assert item.status == "SKIPPED"

    finally:
        engine.dispose()


def test_no_pending_notification(monkeypatch):
    engine, factory = make_test_db(monkeypatch)

    try:
        assert outbox.claim_next_notification() is None

    finally:
        engine.dispose()
