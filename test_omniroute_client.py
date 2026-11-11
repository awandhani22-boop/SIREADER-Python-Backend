
import pytest

from app.ai import omniroute_client as client
from app.ai import provider_gateway


def test_anthropic_usage_cache_dipisahkan():
    usage = {
        "input_tokens": 3000,
        "output_tokens": 500,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 100,
    }

    result = client.normalize_usage(usage)

    assert result == {
        "input_tokens": 3000,
        "output_tokens": 500,
        "cache_read_tokens": 300,
        "cache_write_tokens": 100,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "thinking_tokens": 0,
        "cache_ttl_known": False,
    }


def test_openai_usage_cache_dikurangi_dari_prompt():
    usage = {
        "prompt_tokens": 3000,
        "completion_tokens": 500,
        "prompt_tokens_details": {
            "cached_tokens": 500,
        },
    }

    result = client.normalize_usage(usage)

    assert result == {
        "input_tokens": 2500,
        "output_tokens": 500,
        "cache_read_tokens": 500,
        "cache_write_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "thinking_tokens": 0,
        "cache_ttl_known": False,
    }


def test_openai_usage_mendukung_cache_write():
    usage = {
        "prompt_tokens": 3000,
        "completion_tokens": 500,
        "prompt_tokens_details": {
            "cached_tokens": 400,
            "cache_creation_input_tokens": 100,
        },
    }

    result = client.normalize_usage(usage)

    assert result == {
        "input_tokens": 2500,
        "output_tokens": 500,
        "cache_read_tokens": 400,
        "cache_write_tokens": 100,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "thinking_tokens": 0,
        "cache_ttl_known": False,
    }


def test_anthropic_cache_ttl_split_dan_thinking_metadata():
    usage = {
        "input_tokens": 1200,
        "output_tokens": 900,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 600,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 400,
            "ephemeral_1h_input_tokens": 200,
        },
        "output_tokens_details": {"reasoning_tokens": 250},
    }

    result = client.normalize_usage(usage)

    assert result["cache_write_tokens"] == 600
    assert result["cache_write_5m_tokens"] == 400
    assert result["cache_write_1h_tokens"] == 200
    assert result["cache_ttl_known"] is True
    # Thinking token hanya metadata; output_tokens tidak ditambah.
    assert result["thinking_tokens"] == 250
    assert result["output_tokens"] == 900


def test_thinking_tidak_menggandakan_output_openai():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 800,
        "completion_tokens_details": {"reasoning_tokens": 300},
    }

    result = client.normalize_usage(usage)

    assert result["output_tokens"] == 800
    assert result["thinking_tokens"] == 300


def test_usage_tidak_valid_ditolak():
    with pytest.raises(client.AIUsageMissingError):
        client.normalize_usage(None)


def test_output_tokens_hilang_ditolak():
    usage = {
        "input_tokens": 3000,
    }

    with pytest.raises(client.AIUsageMissingError):
        client.normalize_usage(usage)


def test_token_negatif_ditolak():
    usage = {
        "input_tokens": -100,
        "output_tokens": 500,
    }

    with pytest.raises(client.AIUsageMissingError):
        client.normalize_usage(usage)



def test_worker_pre_request_persistence_failure_releases_cost_reservation(monkeypatch):
    monkeypatch.setattr(provider_gateway.settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(provider_gateway.settings, "ANTHROPIC_API_KEY", "test-only-key")
    monkeypatch.setattr(provider_gateway.settings, "AI_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setattr(
        provider_gateway, "get_tier_config",
        lambda _tier: {"model": "claude-haiku-5-5", "max_output": 700},
    )
    monkeypatch.setattr(
        provider_gateway, "count_input_tokens",
        lambda *_args, **_kwargs: 3,
    )
    monkeypatch.setattr(provider_gateway, "reserve_ai_request", lambda **_kwargs: {
        "request_id": "attempt-request-123",
        "billing_currency": "USD",
    })
    failed = {}
    monkeypatch.setattr(
        provider_gateway,
        "mark_ai_request_failed",
        lambda request_id, error, definitely_not_charged=False: failed.update({
            "request_id": request_id,
            "error": error,
            "definitely_not_charged": definitely_not_charged,
        }),
    )

    def reject_persistence(_request_id):
        raise RuntimeError("database unavailable")

    def unexpected_network_call(*_args, **_kwargs):
        pytest.fail("Paid Messages API request must not be sent after persistence failure.")

    monkeypatch.setattr(provider_gateway, "urlopen", unexpected_network_call)

    with pytest.raises(provider_gateway.AIRequestError) as error:
        provider_gateway.call_ai(
            tier="FAST",
            messages=[{"role": "user", "content": "test"}],
            on_reserved=reject_persistence,
        )

    assert error.value.definitely_not_charged is True
    assert failed["request_id"] == "attempt-request-123"
    assert failed["definitely_not_charged"] is True
