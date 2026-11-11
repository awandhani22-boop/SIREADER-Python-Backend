from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.ai import provider_gateway


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


def test_anthropic_payload_separates_system_and_cache_marks_static_prefix(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROMPT_CACHING_ENABLED", True)
    monkeypatch.setattr(provider_gateway.settings, "AI_PROMPT_CACHE_TTL", "5m")
    payload = provider_gateway.build_anthropic_payload(
        "claude-haiku-5-5",
        [
            {"role": "system", "content": "Static system instructions"},
            {"role": "user", "content": "Dynamic user message"},
        ],
        700,
    )
    assert payload["max_tokens"] == 700
    assert payload["system"][0]["text"] == "Static system instructions"
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert payload["messages"] == [{"role": "user", "content": "Dynamic user message"}]


def test_anthropic_gateway_sends_one_direct_messages_request_and_records_real_usage(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "unit-test-secret")
    monkeypatch.setattr(provider_gateway.settings, "AI_API_KEY", None)
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(provider_gateway.settings, "AI_PROMPT_CACHING_ENABLED", False)
    monkeypatch.setattr(
        provider_gateway,
        "get_tier_config",
        lambda tier: {"model": "claude-haiku-5-5", "max_output": 700},
    )
    reserve = Mock(return_value={"request_id": "req-anthropic-0001", "billing_currency": "USD"})
    finalize = Mock(return_value=Decimal("0.000025"))
    monkeypatch.setattr(provider_gateway, "reserve_ai_request", reserve)
    monkeypatch.setattr(provider_gateway, "finalize_ai_request", finalize)
    monkeypatch.setattr(provider_gateway, "record_budget_alerts", lambda *_args: [])
    data = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "Safe synthetic result."}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 20,
                "ephemeral_1h_input_tokens": 0,
            },
        },
    }
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        if request.full_url.endswith("/count_tokens"):
            return FakeResponse({"input_tokens": 110})
        return FakeResponse(data)

    monkeypatch.setattr(provider_gateway, "urlopen", fake_urlopen)
    result = provider_gateway.call_ai(
        "FAST",
        [
            {"role": "system", "content": "Static instructions"},
            {"role": "user", "content": "Synthetic task"},
        ],
        max_output_tokens=700,
        request_id="req-anthropic-0001",
    )

    assert len(calls) == 2
    count_request, _ = calls[0]
    assert count_request.full_url == "https://api.anthropic.com/v1/messages/count_tokens"
    count_payload = json.loads(count_request.data.decode("utf-8"))
    assert count_payload["model"] == "claude-haiku-5-5"
    assert "max_tokens" not in count_payload
    request, timeout = calls[1]
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == "unit-test-secret"
    assert request.get_header("Anthropic-version") == provider_gateway.settings.ANTHROPIC_API_VERSION
    sent = json.loads(request.data.decode("utf-8"))
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["max_tokens"] == 700
    assert sent["system"] == "Static instructions"
    reserve.assert_called_once()
    assert reserve.call_args.kwargs["model"] == "claude-haiku-5-5"
    assert reserve.call_args.kwargs["estimated_input_tokens"] == 110
    finalize.assert_called_once_with(
        request_id="req-anthropic-0001",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=20,
        cache_write_5m_tokens=20,
        cache_write_1h_tokens=0,
        thinking_tokens=0,
        cache_ttl_known=True,
    )
    assert result["content"] == "Safe synthetic result."
    assert result["finish_reason"] == "end_turn"
    assert result["billing_currency"] == "USD"
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["cache_read_tokens"] == 10
    assert result["preflight_input_tokens"] == 110


def test_anthropic_gateway_refuses_missing_key_before_reservation_or_network(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(provider_gateway.settings, "AI_API_KEY", None)
    reserve = Mock()
    network = Mock()
    monkeypatch.setattr(provider_gateway, "reserve_ai_request", reserve)
    monkeypatch.setattr(provider_gateway, "urlopen", network)
    with pytest.raises(provider_gateway.AIRequestError) as raised:
        provider_gateway.call_ai("FAST", [{"role": "user", "content": "test"}])
    assert raised.value.definitely_not_charged is True
    reserve.assert_not_called()
    network.assert_not_called()


def test_global_provider_gate_rejection_releases_reservation_before_messages_dispatch(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "unit-test-secret")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(
        provider_gateway,
        "get_tier_config",
        lambda tier: {"model": "claude-haiku-5-5", "max_output": 700},
    )
    monkeypatch.setattr(
        provider_gateway,
        "reserve_ai_request",
        lambda **_kwargs: {"request_id": "req-global-slot", "billing_currency": "USD"},
    )
    mark_failed = Mock()
    monkeypatch.setattr(provider_gateway, "mark_ai_request_failed", mark_failed)
    network_calls = []

    def fake_urlopen(request, *_args, **_kwargs):
        network_calls.append(request.full_url)
        return FakeResponse({"input_tokens": 2})

    monkeypatch.setattr(provider_gateway, "urlopen", fake_urlopen)

    class GlobalGateRejected(RuntimeError):
        code = "GLOBAL_PROVIDER_CONCURRENCY_LIMIT"

    def reject_global_slot(_request_id):
        raise GlobalGateRejected("global slot occupied")

    with pytest.raises(provider_gateway.AIRequestError) as raised:
        provider_gateway.call_ai(
            "FAST",
            [{"role": "user", "content": "synthetic"}],
            request_id="req-global-slot",
            on_reserved=reject_global_slot,
        )

    assert raised.value.code == "GLOBAL_PROVIDER_CONCURRENCY_LIMIT"
    assert raised.value.definitely_not_charged is True
    assert network_calls == ["https://api.anthropic.com/v1/messages/count_tokens"]
    mark_failed.assert_called_once_with(
        "req-global-slot",
        "Pre-request state persistence failed: GlobalGateRejected",
        definitely_not_charged=True,
    )


def test_anthropic_gateway_never_retries_after_unknown_transport_outcome(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "unit-test-secret")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(
        provider_gateway,
        "get_tier_config",
        lambda tier: {"model": "claude-haiku-5-5", "max_output": 700},
    )
    monkeypatch.setattr(
        provider_gateway,
        "reserve_ai_request",
        lambda **kwargs: {"request_id": "req-unknown", "billing_currency": "USD"},
    )
    mark_failed = Mock()
    monkeypatch.setattr(provider_gateway, "mark_ai_request_failed", mark_failed)
    calls = []

    def timeout_once(request, *_args, **_kwargs):
        if request.full_url.endswith("/count_tokens"):
            calls.append("counted")
            return FakeResponse({"input_tokens": 2})
        calls.append("messages-dispatched")
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(provider_gateway, "urlopen", timeout_once)
    with pytest.raises(provider_gateway.AIRequestError):
        provider_gateway.call_ai("FAST", [{"role": "user", "content": "test"}], request_id="req-unknown")
    assert calls == ["counted", "messages-dispatched"]
    mark_failed.assert_called_once_with("req-unknown", "TimeoutError", False)


def test_anthropic_usage_normalizer_tracks_cache_categories_and_rejects_negative():
    usage = provider_gateway.normalize_usage({
        "input_tokens": 40,
        "output_tokens": 12,
        "cache_read_input_tokens": 8,
        "cache_creation_input_tokens": 9,
        "cache_creation": {"ephemeral_5m_input_tokens": 9, "ephemeral_1h_input_tokens": 0},
    })
    assert usage["input_tokens"] == 40
    assert usage["cache_read_tokens"] == 8
    assert usage["cache_write_5m_tokens"] == 9
    with pytest.raises(provider_gateway.AIUsageMissingError):
        provider_gateway.normalize_usage({"input_tokens": -1, "output_tokens": 1})


def test_token_count_failure_blocks_reservation_and_paid_message_request(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "unit-test-secret")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(
        provider_gateway, "get_tier_config",
        lambda _tier: {"model": "claude-haiku-5-5", "max_output": 700},
    )
    reserve = Mock()
    calls = []
    def fail_count(request, *_args, **_kwargs):
        calls.append(request.full_url)
        raise TimeoutError("count endpoint unavailable")
    monkeypatch.setattr(provider_gateway, "reserve_ai_request", reserve)
    monkeypatch.setattr(provider_gateway, "urlopen", fail_count)

    with pytest.raises(provider_gateway.AIRequestError) as raised:
        provider_gateway.call_ai("FAST", [{"role": "user", "content": "synthetic"}])

    assert raised.value.code == "TOKEN_COUNT_FAILED"
    assert raised.value.definitely_not_charged is True
    assert calls == ["https://api.anthropic.com/v1/messages/count_tokens"]
    reserve.assert_not_called()


def test_anthropic_token_count_uses_the_exact_model_and_complete_payload(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://api.anthropic.com")
    payload = {
        "model": "claude-haiku-5-5",
        "max_tokens": 700,
        "system": [{"type": "text", "text": "static instructions"}],
        "messages": [{"role": "user", "content": "dynamic message"}],
    }
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        return FakeResponse({"input_tokens": 123})

    original = provider_gateway.urlopen
    try:
        provider_gateway.urlopen = fake_urlopen
        count = provider_gateway.count_input_tokens(
            "claude-haiku-5-5", payload, api_key="test-key"
        )
    finally:
        provider_gateway.urlopen = original

    assert count == 123
    assert captured["url"] == "https://api.anthropic.com/v1/messages/count_tokens"
    assert captured["payload"]["model"] == "claude-haiku-5-5"
    assert captured["payload"]["system"] == payload["system"]
    assert captured["payload"]["messages"] == payload["messages"]
    assert "max_tokens" not in captured["payload"]
    assert captured["headers"]["X-api-key"] == "test-key"


def test_anthropic_gateway_rejects_non_official_https_endpoint_before_sending_key(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "test-key")
    network = Mock()
    monkeypatch.setattr(provider_gateway, "urlopen", network)
    with pytest.raises(provider_gateway.AIRequestError) as raised:
        provider_gateway.call_ai("FAST", [{"role": "user", "content": "test"}])
    assert raised.value.code == "ANTHROPIC_ENDPOINT_NOT_OFFICIAL"
    assert raised.value.definitely_not_charged is True
    network.assert_not_called()


def test_omniroute_provider_accepts_only_verified_loopback_gateway(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "omniroute")
    provider_gateway._validate_endpoint("http://127.0.0.1:20128/v1/messages")
    provider_gateway._validate_endpoint("http://localhost:20128/v1/messages")

    for endpoint in (
        "http://127.0.0.1:20129/v1/messages",
        "http://192.168.1.20:20128/v1/messages",
        "https://proxy.example.test/v1/messages",
    ):
        with pytest.raises(provider_gateway.AIRequestError) as raised:
            provider_gateway._validate_endpoint(endpoint)
        assert raised.value.definitely_not_charged is True
        assert raised.value.code == "ANTHROPIC_ENDPOINT_NOT_OFFICIAL"

def test_omniroute_uses_gateway_key_not_upstream_anthropic_key(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "omniroute")
    monkeypatch.setattr(provider_gateway.settings, "AI_API_KEY", "omniroute-test-key")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "upstream-test-key")
    assert provider_gateway._api_key() == "omniroute-test-key"


def test_omniroute_gateway_uses_native_messages_routes_and_gateway_key(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "omniroute")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setattr(provider_gateway.settings, "AI_API_KEY", "omniroute-test-key")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "upstream-key-must-not-be-used")
    monkeypatch.setattr(provider_gateway.settings, "AI_PROMPT_CACHING_ENABLED", False)
    monkeypatch.setattr(
        provider_gateway,
        "get_tier_config",
        lambda _tier: {"model": "anthropic/claude-haiku-5-5", "max_output": 700},
    )
    reserve = Mock(return_value={"request_id": "req-omni-0001", "billing_currency": "USD"})
    finalize = Mock(return_value=Decimal("0.000025"))
    monkeypatch.setattr(provider_gateway, "reserve_ai_request", reserve)
    monkeypatch.setattr(provider_gateway, "finalize_ai_request", finalize)
    monkeypatch.setattr(provider_gateway, "record_budget_alerts", lambda *_args: [])
    response_data = {
        "id": "msg_omni_test",
        "type": "message",
        "role": "assistant",
        "model": "anthropic/claude-haiku-5-5",
        "content": [{"type": "text", "text": "Synthetic OmniRoute result."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/count_tokens"):
            return FakeResponse({"input_tokens": 12})
        return FakeResponse(response_data)

    monkeypatch.setattr(provider_gateway, "urlopen", fake_urlopen)
    result = provider_gateway.call_ai(
        "FAST", [{"role": "user", "content": "Synthetic offline check"}],
        request_id="req-omni-0001",
    )

    assert [request.full_url for request in requests] == [
        "http://127.0.0.1:20128/v1/messages/count_tokens",
        "http://127.0.0.1:20128/v1/messages",
    ]
    assert all(request.get_header("X-api-key") == "omniroute-test-key" for request in requests)
    assert result["provider"] == "omniroute"
    assert result["model"] == "anthropic/claude-haiku-5-5"
    assert result["billing_currency"] == "USD"
    reserve.assert_called_once()
    finalize.assert_called_once()

def test_anthropic_mode_never_falls_back_to_legacy_omniroute_key(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(provider_gateway.settings, "AI_API_KEY", "omniroute-key-must-not-leave-local-gateway")
    assert provider_gateway._api_key() is None


def test_anthropic_mode_uses_only_the_dedicated_anthropic_key(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setattr(provider_gateway.settings, "AI_API_KEY", "different-omniroute-key")
    assert provider_gateway._api_key() == "anthropic-test-key"
