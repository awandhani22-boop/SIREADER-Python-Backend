from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai import provider_gateway
from app.ai.cost_guard import BudgetExceededError, _validate_task_cost_limit
from app.config import settings
from app.workers.provider import (
    AnthropicWorkerProvider,
    MockWorkerProvider,
    WorkerExecutionRequest,
    WorkerProviderError,
    create_worker_provider,
)
from app.workers.registry import build_default_registry


def _request(cancel=None, model_tier="FAST"):
    return WorkerExecutionRequest(
        task_id="task_12345678",
        task_type="SUMMARIZATION",
        title="Summarize a supplied document",
        input_payload={"text": "example"},
        correlation_id="corr_12345678",
        attempt_request_id="a" * 32,
        model_tier=model_tier,
        cancellation=cancel,
    )


def _enable_live(monkeypatch, cap=Decimal("3.00")):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-only-placeholder")
    monkeypatch.setattr(settings, "AI_API_KEY", None)
    monkeypatch.setattr(settings, "WORKER_MAX_TASK_COST_USD", cap)


def test_mock_provider_is_deterministic_and_explicitly_simulated():
    worker = build_default_registry()["haiku"]
    provider = MockWorkerProvider()
    first = provider.execute(worker, _request())
    second = provider.execute(worker, _request())
    assert first == second
    assert first.source == "SIMULATED"
    assert first.provider_request_id is None
    assert "api_key" not in first.result_payload


def test_mock_provider_honors_pre_execution_cancellation():
    from threading import Event
    event = Event()
    event.set()
    with pytest.raises(WorkerProviderError) as error:
        MockWorkerProvider().execute(build_default_registry()["haiku"], _request(event))
    assert error.value.code == "CANCELLED_BEFORE_EXECUTION"
    assert error.value.outcome_unknown is False


def test_factory_is_closed_when_runtime_disabled(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", False)
    with pytest.raises(WorkerProviderError) as error:
        create_worker_provider(build_default_registry()["haiku"])
    assert error.value.code == "WORKER_RUNTIME_DISABLED"


def test_anthropic_worker_requires_independent_live_enablement(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", False)
    with pytest.raises(WorkerProviderError) as error:
        create_worker_provider(build_default_registry()["haiku"])
    assert error.value.code == "LIVE_PROVIDER_DISABLED"


def test_python_profiles_do_not_require_paid_provider_switch(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", False)
    assert create_worker_provider(build_default_registry()["market_monitor"]).__class__.__name__ == "PythonWorkerProvider"


def test_live_provider_requires_positive_per_task_cap(monkeypatch):
    _enable_live(monkeypatch, Decimal("0"))
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "TASK_COST_CAP_NOT_CONFIGURED"


def test_live_provider_requires_api_key_before_dispatch(monkeypatch):
    _enable_live(monkeypatch)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(settings, "AI_API_KEY", None)
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "PROVIDER_NOT_CONFIGURED"


def test_live_provider_passes_stable_request_id_task_cap_and_agreed_output_limit(monkeypatch):
    _enable_live(monkeypatch)
    captured = {}

    def fake_call_ai(**kwargs):
        captured.update(kwargs)
        return {
            "content": "safe result",
            "tier": "FAST",
            "model": "claude-haiku-5-5",
            "request_id": kwargs["request_id"],
            "billing_currency": "USD",
            "cost": Decimal("0.001"),
            "finish_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
            },
        }

    monkeypatch.setattr(provider_gateway, "call_ai", fake_call_ai)
    result = AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert result.source == "LIVE"
    assert captured["request_id"] == "a" * 32
    assert captured["max_output_tokens"] == 700
    assert captured["max_cost_usd"] == Decimal("3.00")
    assert result.provider_request_id == "a" * 32
    assert result.billing_currency == "USD"
    assert result.cost_amount == "0.001"
    assert result.usage["input_tokens"] == 10


def test_worker_without_ai_profile_cannot_call_anthropic(monkeypatch):
    _enable_live(monkeypatch)
    worker = build_default_registry()["market_monitor"]
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(worker, _request())
    assert error.value.code == "WORKER_AI_NOT_ALLOWED"


def test_task_cost_guard_enforces_currency_and_reserved_estimate():
    _validate_task_cost_limit("IDR", Decimal("1.00"), max_cost_rp=Decimal("1.00"))
    with pytest.raises(BudgetExceededError, match="per-task"):
        _validate_task_cost_limit("IDR", Decimal("1.01"), max_cost_rp=Decimal("1.00"))
    with pytest.raises(ValueError, match="USD task cap"):
        _validate_task_cost_limit("IDR", Decimal("1.00"), max_cost_usd=Decimal("1.00"))
    with pytest.raises(ValueError, match="negative"):
        _validate_task_cost_limit("USD", Decimal("0.01"), max_cost_usd=Decimal("-1"))


def test_pre_request_persistence_failure_is_known_and_safely_retryable(monkeypatch):
    _enable_live(monkeypatch)

    def reject_persistence(**_kwargs):
        raise provider_gateway.AIRequestError(
            "pre-request persistence failed",
            definitely_not_charged=True,
        )

    monkeypatch.setattr(provider_gateway, "call_ai", reject_persistence)
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "PROVIDER_STATE_PERSIST_FAILED"
    assert error.value.retryable is True
    assert error.value.outcome_unknown is False


def test_budget_guard_rejection_is_known_and_never_retried(monkeypatch):
    _enable_live(monkeypatch)

    def reject_budget(**_kwargs):
        raise BudgetExceededError("per-task budget exceeded")

    monkeypatch.setattr(provider_gateway, "call_ai", reject_budget)
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "BUDGET_BLOCKED"
    assert error.value.outcome_unknown is False
    assert error.value.retryable is False


@pytest.mark.parametrize("failure", [
    TimeoutError("provider timed out"),
    provider_gateway.AIRequestError("provider rejected after dispatch", definitely_not_charged=False),
])
def test_provider_timeout_or_post_dispatch_error_is_unknown_and_not_retryable(monkeypatch, failure):
    _enable_live(monkeypatch)

    def fail_after_dispatch(**_kwargs):
        raise failure

    monkeypatch.setattr(provider_gateway, "call_ai", fail_after_dispatch)
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "PROVIDER_OUTCOME_UNKNOWN"
    assert error.value.outcome_unknown is True
    assert error.value.retryable is False


def test_provider_success_without_usage_is_unknown_and_not_retried(monkeypatch):
    _enable_live(monkeypatch)
    monkeypatch.setattr(provider_gateway, "call_ai", lambda **kwargs: {
        "content": "completed but usage missing",
        "tier": "FAST",
        "model": "claude-haiku-5-5",
        "request_id": kwargs["request_id"],
        "billing_currency": "USD",
        "cost": Decimal("0.001"),
        "usage": None,
    })
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "PROVIDER_USAGE_UNAVAILABLE"
    assert error.value.outcome_unknown is True
    assert error.value.retryable is False


def test_live_provider_fails_closed_on_missing_or_mismatched_policy_tier(monkeypatch):
    _enable_live(monkeypatch)
    worker = build_default_registry()["haiku"]
    for tier in ("NONE", "REASONING", "UNRECOGNIZED"):
        with pytest.raises(WorkerProviderError) as error:
            AnthropicWorkerProvider().execute(worker, _request(model_tier=tier))
        assert error.value.code == "MODEL_TIER_POLICY_REJECTED"

def test_live_provider_refuses_legacy_omniroute_mode_even_with_anthropic_key(monkeypatch):
    _enable_live(monkeypatch)
    monkeypatch.setattr(settings, "AI_PROVIDER", "omniroute")
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "ANTHROPIC_PROVIDER_REQUIRED"


def test_live_provider_never_uses_legacy_omniroute_key_as_anthropic_credential(monkeypatch):
    _enable_live(monkeypatch)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(settings, "AI_API_KEY", "omniroute-key-must-not-leave-local-gateway")
    with pytest.raises(WorkerProviderError) as error:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], _request())
    assert error.value.code == "PROVIDER_NOT_CONFIGURED"
