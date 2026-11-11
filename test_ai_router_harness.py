from __future__ import annotations

from app.ai import ai_router


def _commentary_bundle(**overrides):
    message = {
        "telegram_message_id": 7001,
        "text": "Selamat pagi semuanya",
        "message_roles": ["COMMENTARY"],
        "message_role": "COMMENTARY",
        "reply_to_id": None,
        "trade_episode_id": None,
        "trade_episode_status": None,
        "edit_history": [],
        "is_edited": False,
    }
    message.update(overrides)
    return {"target_message_id": 7001, "messages": [message]}


def test_python_prefilter_skips_only_explicit_pure_commentary(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Model must not be called for explicit pure commentary.")

    monkeypatch.setattr(ai_router, "_run_tier", fail_if_called)
    result = ai_router.analyze_bundle(_commentary_bundle())
    assert result["status"] == "NO_SIGNAL"
    assert result["attempts"] == []
    assert result["routing"] == {
        "mode": "PYTHON_FILTER",
        "reason_code": "PURE_COMMENTARY",
        "model_calls": 0,
    }


def test_python_prefilter_fails_open_for_unknown_or_trade_relevant_context():
    assert ai_router._bundle_is_pure_commentary(_commentary_bundle()) is True
    assert ai_router._bundle_is_pure_commentary(
        _commentary_bundle(message_roles=["UNKNOWN"], message_role="UNKNOWN")
    ) is False
    assert ai_router._bundle_is_pure_commentary(
        _commentary_bundle(reply_to_id=7000)
    ) is False
    assert ai_router._bundle_is_pure_commentary(
        _commentary_bundle(trade_episode_id="episode-123")
    ) is False
    assert ai_router._bundle_is_pure_commentary(
        _commentary_bundle(edit_history=[{"old_text": "hi", "new_text": "buy"}])
    ) is False
    assert ai_router._bundle_is_pure_commentary(
        _commentary_bundle(message_roles=["COMMENTARY", "POSITION_UPDATE"])
    ) is False


def test_model_output_limit_stops_routing_and_requires_manual_review(monkeypatch):
    signal_bundle = {
        "target_message_id": 8001,
        "messages": [{
            "telegram_message_id": 8001,
            "text": "Buy zone 4000-3995",
            "message_roles": ["NEW_SIGNAL", "PRICE_ZONE"],
            "message_role": "NEW_SIGNAL",
        }],
    }
    monkeypatch.setattr(ai_router, "build_messages", lambda *_args, **_kwargs: [{"role": "user", "content": "synthetic"}])
    monkeypatch.setattr(ai_router, "can_use_expensive_tier", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ai_router, "_run_tier", lambda *_args, **_kwargs: {
        "analysis": {"content": "{", "finish_reason": "max_tokens", "usage": {}, "model": "test"},
        "parsed": None,
    })
    result = ai_router.analyze_bundle(signal_bundle)
    assert result["status"] == "MANUAL_REVIEW"
    assert result["reason"] == "MODEL_OUTPUT_LIMIT_REACHED"
    assert result["truncated_tier"] == "FAST"
    assert result["partial_output_is_authoritative"] is False
    assert len(result["attempts"]) == 1
