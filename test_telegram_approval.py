import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.approval import telegram_bot


def make_update(callback_data):
    query = SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    user = SimpleNamespace(
        id=123456,
        full_name="Test Reviewer",
    )

    chat = SimpleNamespace(
        id=123456,
        type="private",
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=user,
        effective_chat=chat,
        effective_message=None,
    )

    context = SimpleNamespace(user_data={})

    return update, context, query


def make_pending_detail():
    return {
        "analysis_log_id": 5,
        "target_message_id": 20601,
        "status": "MANUAL_REVIEW",
        "final_analysis": {
            "confidence": 0.95,
            "extracted": {
                "direction": "BUY",
                "entry_zone_text": "3922-3920",
                "stop_loss_text": "Sl20/30 pips",
                "take_profit_texts": [],
            },
            "uncertainties": [
                "TP belum disebutkan.",
                "SL masih ambigu.",
            ],
        },
    }


def test_approve_hanya_membuka_konfirmasi(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "is_authorized",
        lambda update: True,
    )

    monkeypatch.setattr(
        telegram_bot,
        "get_review_detail",
        lambda analysis_id: make_pending_detail(),
    )

    def must_not_approve(**kwargs):
        pytest.fail("Keputusan tidak boleh disimpan sebelum konfirmasi.")

    monkeypatch.setattr(
        telegram_bot,
        "decide_review",
        must_not_approve,
    )

    update, context, query = make_update("approve:5")

    asyncio.run(
        telegram_bot.callback_router(update, context)
    )

    query.edit_message_text.assert_awaited_once()

    message = query.edit_message_text.await_args.args[0]
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]

    assert "KONFIRMASI APPROVAL" in message
    assert "BUY" in message
    assert "SL masih ambigu" in message

    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "confirmapprove:5" in callbacks
    assert "review:5" in callbacks


def test_konfirmasi_baru_menyimpan_approval(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "is_authorized",
        lambda update: True,
    )

    monkeypatch.setattr(
        telegram_bot,
        "get_review_detail",
        lambda analysis_id: make_pending_detail(),
    )

    calls = []

    def fake_decide_review(**kwargs):
        calls.append(kwargs)

        return {
            "reviewer": kwargs["reviewer"],
            "new_status": "APPROVED",
        }

    monkeypatch.setattr(
        telegram_bot,
        "decide_review",
        fake_decide_review,
    )

    update, context, query = make_update("confirmapprove:5")

    asyncio.run(
        telegram_bot.callback_router(update, context)
    )

    assert len(calls) == 1
    assert calls[0]["analysis_log_id"] == 5
    assert calls[0]["decision"] == "APPROVED"
    assert calls[0]["reviewer"] == "Test Reviewer"

    message = query.edit_message_text.await_args.args[0]

    assert "REVIEW APPROVED" in message
    assert "Tidak ada order MT5" in message
