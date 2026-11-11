from __future__ import annotations

from decimal import Decimal
from threading import Event

import pytest

from app.ai import provider_gateway
from app.config import settings
from app.workers import provider
from app.workers.provider import (
    AnthropicWorkerProvider,
    MockWorkerProvider,
    PythonWorkerProvider,
    WorkerExecutionRequest,
    WorkerProviderError,
    create_worker_provider,
)
from app.workers.registry import build_default_registry


def make_request(**overrides) -> WorkerExecutionRequest:
    values = {
        "task_id": "task_00000001",
        "task_type": "QUICK_RESEARCH",
        "required_capability": "quick_research",
        "title": "Summarize a synthetic sample",
        "input_payload": {"text": "A harmless synthetic test paragraph."},
        "correlation_id": "corr_00000001",
        "attempt_request_id": "attempt_00000001",
        "model_tier": "FAST",
    }
    values.update(overrides)
    return WorkerExecutionRequest(**values)


def enable_hybrid(monkeypatch, *, live=False, cap=Decimal("0")):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", live)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "unit-test-only" if live else None)
    monkeypatch.setattr(settings, "AI_API_KEY", None)
    monkeypatch.setattr(settings, "WORKER_MAX_TASK_COST_USD", cap)


def test_mock_provider_is_deterministic_and_never_reports_billing() -> None:
    worker = build_default_registry()["haiku"]
    request = make_request()
    first = MockWorkerProvider().execute(worker, request)
    second = MockWorkerProvider().execute(worker, request)
    assert first == second
    assert first.source == "SIMULATED"
    assert first.billing_currency is None
    assert first.usage is None


def test_mock_provider_honors_cancellation_before_execution() -> None:
    cancelled = Event()
    cancelled.set()
    with pytest.raises(WorkerProviderError) as raised:
        MockWorkerProvider().execute(build_default_registry()["haiku"], make_request(cancellation=cancelled))
    assert raised.value.code == "CANCELLED_BEFORE_EXECUTION"


def test_provider_factory_fails_closed_when_runtime_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", False)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "MOCK")
    with pytest.raises(WorkerProviderError) as raised:
        create_worker_provider(build_default_registry()["haiku"])
    assert raised.value.code == "WORKER_RUNTIME_DISABLED"


def test_provider_factory_selects_mock_without_credentials_or_network(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "MOCK")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", False)
    monkeypatch.setattr(settings, "AI_API_KEY", None)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    assert isinstance(create_worker_provider(build_default_registry()["haiku"]), MockWorkerProvider)


def test_provider_factory_routes_python_profiles_without_live_model_gate(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=False)
    assert isinstance(create_worker_provider(build_default_registry()["data_archivist"]), PythonWorkerProvider)


def test_provider_factory_requires_live_switch_for_ai_profiles(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=False)
    with pytest.raises(WorkerProviderError) as raised:
        create_worker_provider(build_default_registry()["haiku"])
    assert raised.value.code == "LIVE_PROVIDER_DISABLED"


def test_provider_factory_selects_shared_anthropic_provider_when_explicitly_enabled(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True)
    assert isinstance(create_worker_provider(build_default_registry()["haiku"]), AnthropicWorkerProvider)


def test_provider_factory_rejects_unknown_and_legacy_provider_modes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    for mode, code in (("UNRECOGNIZED", "UNKNOWN_PROVIDER_MODE"), ("OMNIROUTE", "UNKNOWN_PROVIDER_MODE")):
        monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", mode)
        with pytest.raises(WorkerProviderError) as raised:
            create_worker_provider(build_default_registry()["haiku"])
        assert raised.value.code == code


def test_anthropic_provider_rejects_tier_mismatch_before_gateway(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True, cap=Decimal("1"))
    calls = []
    monkeypatch.setattr(provider_gateway, "call_ai", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(WorkerProviderError) as raised:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], make_request(model_tier="REASONING"))
    assert raised.value.code == "MODEL_TIER_POLICY_REJECTED"
    assert calls == []


def test_anthropic_provider_rejects_zero_task_cap_before_gateway(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True, cap=Decimal("0"))
    calls = []
    monkeypatch.setattr(provider_gateway, "call_ai", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(WorkerProviderError) as raised:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], make_request())
    assert raised.value.code == "TASK_COST_CAP_NOT_CONFIGURED"
    assert calls == []


def test_anthropic_provider_uses_profile_model_output_ceiling_and_usd_cap(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True, cap=Decimal("0.25"))
    calls = []

    def fake_call_ai(**kwargs):
        calls.append(kwargs)
        return {
            "content": "Synthetic result",
            "tier": "FAST",
            "model": "claude-haiku-5-5",
            "request_id": kwargs["request_id"],
            "billing_currency": "USD",
            "cost": Decimal("0.01"),
            "finish_reason": "end_turn",
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
            },
        }

    monkeypatch.setattr(provider_gateway, "call_ai", fake_call_ai)
    result = AnthropicWorkerProvider().execute(build_default_registry()["haiku"], make_request())
    assert len(calls) == 1
    assert calls[0]["max_output_tokens"] == 700
    assert calls[0]["max_cost_usd"] == Decimal("0.25")
    assert result.source == "LIVE"
    assert result.billing_currency == "USD"
    assert result.usage["output_tokens"] == 5


def test_global_provider_gate_rejection_is_retryable_and_never_dispatched(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True, cap=Decimal("0.25"))
    calls = []

    def reject_before_dispatch(**_kwargs):
        calls.append("pre-request")
        raise provider_gateway.AIRequestError(
            "global provider slot occupied",
            definitely_not_charged=True,
            code="GLOBAL_PROVIDER_CONCURRENCY_LIMIT",
        )

    monkeypatch.setattr(provider_gateway, "call_ai", reject_before_dispatch)
    with pytest.raises(WorkerProviderError) as raised:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], make_request())
    assert calls == ["pre-request"]
    assert raised.value.code == "GLOBAL_PROVIDER_CONCURRENCY_LIMIT"
    assert raised.value.retryable is True
    assert raised.value.outcome_unknown is False


def test_anthropic_provider_does_not_retry_unknown_outcome(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True, cap=Decimal("0.25"))
    calls = []

    def fail_once(**_kwargs):
        calls.append("one")
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(provider_gateway, "call_ai", fail_once)
    with pytest.raises(WorkerProviderError) as raised:
        AnthropicWorkerProvider().execute(build_default_registry()["haiku"], make_request())
    assert calls == ["one"]
    assert raised.value.code == "PROVIDER_OUTCOME_UNKNOWN"
    assert raised.value.outcome_unknown is True


def test_direct_anthropic_provider_cannot_run_deterministic_worker(monkeypatch) -> None:
    enable_hybrid(monkeypatch, live=True, cap=Decimal("0.25"))
    with pytest.raises(WorkerProviderError) as raised:
        AnthropicWorkerProvider().execute(
            build_default_registry()["market_monitor"],
            make_request(model_tier="NONE"),
        )
    assert raised.value.code == "WORKER_AI_NOT_ALLOWED"
