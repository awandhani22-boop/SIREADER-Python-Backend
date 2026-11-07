import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.approval import telegram_bot


def _prepare_post_init(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "ensure_pending_review_notifications",
        lambda recipient: 0,
    )
    monkeypatch.setattr(
        telegram_bot,
        "list_pending_notification_ids",
        lambda: [],
    )
    monkeypatch.setattr(
        telegram_bot,
        "_start_notification_worker",
        lambda application: None,
    )


def test_trade_decision_watch_default_disabled():
    assert (
        Settings.model_fields[
            "TRADE_DECISION_WATCH_ENABLED"
        ].default
        is False
    )


def test_post_init_tidak_memulai_watch_worker_saat_disabled(
    monkeypatch,
):
    calls = []
    application = SimpleNamespace(bot_data={})

    _prepare_post_init(monkeypatch)

    monkeypatch.setattr(
        telegram_bot.settings,
        "TRADE_DECISION_WATCH_ENABLED",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        telegram_bot.trade_decision_watch_worker,
        "start_watch_worker",
        lambda app: calls.append("START"),
    )

    asyncio.run(telegram_bot.post_init(application))

    assert calls == []


def test_post_init_memulai_watch_worker_saat_enabled(
    monkeypatch,
):
    calls = []
    application = SimpleNamespace(bot_data={})

    _prepare_post_init(monkeypatch)

    monkeypatch.setattr(
        telegram_bot.settings,
        "TRADE_DECISION_WATCH_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        telegram_bot.trade_decision_watch_worker,
        "start_watch_worker",
        lambda app: calls.append("START"),
    )

    asyncio.run(telegram_bot.post_init(application))

    assert calls == ["START"]


def test_post_stop_selalu_menghentikan_watch_worker(
    monkeypatch,
):
    calls = []
    application = SimpleNamespace(bot_data={})

    async def fake_stop(app):
        calls.append("STOP")

    monkeypatch.setattr(
        telegram_bot.trade_decision_watch_worker,
        "stop_watch_worker",
        fake_stop,
    )

    asyncio.run(telegram_bot.post_stop(application))

    assert calls == ["STOP"]
