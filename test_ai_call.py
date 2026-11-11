
import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.ai import provider_gateway as client


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.data


def test_call_ai_sukses_tanpa_api_key_sungguhan(monkeypatch):
    monkeypatch.setattr(client.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(client.settings, "ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setattr(client.settings, "AI_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(client.settings, "AI_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(client.settings, "AI_PROMPT_CACHING_ENABLED", False)
    monkeypatch.setattr(
        client, "get_tier_config",
        lambda tier: {"model": "claude-haiku-5-5", "max_output": 700},
    )

    reserve_mock = Mock(return_value={
        "request_id": "test-request-001",
        "billing_currency": "USD",
    })
    monkeypatch.setattr(client, "reserve_ai_request", reserve_mock)

    count_response = {"input_tokens": 41}
    message_response = {
        "id": "msg_test_001",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "Analisis berhasil."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 41,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    urlopen_mock = Mock(side_effect=[
        FakeResponse(count_response),
        FakeResponse(message_response),
    ])
    monkeypatch.setattr(client, "urlopen", urlopen_mock)

    finalize_mock = Mock(return_value=Decimal("0.000020"))
    monkeypatch.setattr(client, "finalize_ai_request", finalize_mock)
    monkeypatch.setattr(client, "record_budget_alerts", lambda *_args: [])

    result = client.call_ai(
        tier="FAST",
        messages=[{"role": "user", "content": "Tes SIREADER"}],
        max_output_tokens=32,
    )

    assert result["request_id"] == "test-request-001"
    assert result["tier"] == "FAST"
    assert result["model"] == "claude-haiku-5-5"
    assert result["content"] == "Analisis berhasil."
    assert result["cost"] == Decimal("0.000020")
    assert result["cost_usd"] == Decimal("0.000020")
    assert result["billing_currency"] == "USD"
    assert result["preflight_input_tokens"] == 41

    reserve_mock.assert_called_once()
    assert reserve_mock.call_args.kwargs["estimated_input_tokens"] == 41
    finalize_mock.assert_called_once()
    assert urlopen_mock.call_count == 2
    assert urlopen_mock.call_args_list[0].args[0].full_url.endswith("/v1/messages/count_tokens")
    assert urlopen_mock.call_args_list[1].args[0].full_url.endswith("/v1/messages")
