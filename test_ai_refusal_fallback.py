import pytest

from app.ai import ai_router


def make_bundle():
    return {
        "chat_id": -1003014486607,
        "target_message_id": 20519,
        "window_minutes": 10,
        "messages": [
            {
                "telegram_message_id": 20519,
                "telegram_date": "2026-11-03T11:00:00+00:00",
                "reply_to_id": None,
                "text": "3960-3962 tunggu cisd m1",
                "is_edited": False,
                "media": [],
                "edit_history": [],
            }
        ],
    }


def make_json_response(
    status="CLEAR",
    confidence=0.95,
    needs_reasoning=False,
    needs_escalation=False,
    critical_ambiguity=False,
):
    import json

    return json.dumps(
        {
            "status": status,
            "confidence": confidence,
            "needs_reasoning": needs_reasoning,
            "needs_escalation": needs_escalation,
            "critical_ambiguity": critical_ambiguity,
            "summary": "Catatan sesuai pesan sumber.",
            "extracted": {
                "direction": None,
                "entry_zone_text": None,
                "explicit_entry_text": None,
                "stop_loss_text": None,
                "take_profit_texts": [],
                "conditions": [],
                "action_instructions": [],
            },
            "evidence": [],
            "uncertainties": [],
        }
    )


def provider_response(content, model):
    return {
        "content": content,
        "model": model,
        "cost_rp": "1.25",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def test_detects_provider_refusal_prefix():
    assert ai_router._is_model_refusal(
        "I can't help with this request."
    )
    assert ai_router._is_model_refusal(
        "I'm not able to assist with that."
    )


def test_does_not_classify_json_as_refusal():
    assert not ai_router._is_model_refusal(
        '{"summary":"I cannot confirm the direction", "status":"CLEAR"}'
    )


def test_fast_refusal_falls_back_to_sonnet_once(monkeypatch):
    outputs = [
        provider_response("I can't help with this request.", "haiku-test"),
        provider_response(
            make_json_response(
                status="NEEDS_REASONING",
                confidence=0.52,
                needs_reasoning=True,
                critical_ambiguity=True,
            ),
            "sonnet-test",
        ),
    ]
    called_tiers = []

    def fake_call_ai(tier, messages, max_output_tokens):
        called_tiers.append(tier)
        return outputs.pop(0)

    monkeypatch.setattr(ai_router, "call_ai", fake_call_ai)
    monkeypatch.setattr(ai_router, "can_use_expensive_tier", lambda tier, messages: True)

    result = ai_router.analyze_bundle(make_bundle())

    assert called_tiers == ["FAST", "REASONING"]
    assert result["fallback_from"] == "FAST_REFUSAL"
    assert result["status"] == "MANUAL_REVIEW"
    assert result["opus_skipped_after_refusal_fallback"] is True
    assert len(result["attempts"]) == 2


def test_fast_and_sonnet_refusal_return_refused_without_opus(monkeypatch):
    outputs = [
        provider_response("I can't help with this request.", "haiku-test"),
        provider_response("I cannot assist with that request.", "sonnet-test"),
    ]
    called_tiers = []

    def fake_call_ai(tier, messages, max_output_tokens):
        called_tiers.append(tier)
        return outputs.pop(0)

    monkeypatch.setattr(ai_router, "call_ai", fake_call_ai)
    monkeypatch.setattr(ai_router, "can_use_expensive_tier", lambda tier, messages: True)

    result = ai_router.analyze_bundle(make_bundle())

    assert called_tiers == ["FAST", "REASONING"]
    assert result["status"] == "REFUSED"
    assert result["refused_tier"] == "REASONING"


def test_fast_refusal_does_not_call_sonnet_when_soft_budget_blocks(monkeypatch):
    called_tiers = []

    def fake_call_ai(tier, messages, max_output_tokens):
        called_tiers.append(tier)
        return provider_response("I can't help with this request.", "haiku-test")

    monkeypatch.setattr(ai_router, "call_ai", fake_call_ai)
    monkeypatch.setattr(ai_router, "can_use_expensive_tier", lambda tier, messages: False)

    result = ai_router.analyze_bundle(make_bundle())

    assert called_tiers == ["FAST"]
    assert result["status"] == "MANUAL_REVIEW_BUDGET"
    assert "FALLBACK_BLOCKED_BY_SOFT_BUDGET" in result["reason"]


def test_needs_reasoning_is_not_marked_analyzed():
    parsed = {
        "status": "NEEDS_REASONING",
        "confidence": 0.52,
        "needs_reasoning": True,
        "needs_escalation": False,
        "critical_ambiguity": True,
    }
    assert ai_router._final_analysis_status(parsed) == "MANUAL_REVIEW"


def test_clear_high_confidence_is_analyzed():
    parsed = {
        "status": "CLEAR",
        "confidence": 0.95,
        "needs_reasoning": False,
        "needs_escalation": False,
        "critical_ambiguity": False,
    }
    assert ai_router._final_analysis_status(parsed) == "ANALYZED"


def test_no_signal_keeps_no_signal_status():
    parsed = {
        "status": "NO_SIGNAL",
        "confidence": 1.0,
        "needs_reasoning": False,
        "needs_escalation": False,
        "critical_ambiguity": False,
    }
    assert ai_router._final_analysis_status(parsed) == "NO_SIGNAL"
