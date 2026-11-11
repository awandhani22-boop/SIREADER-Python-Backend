from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.workers.harness import HarnessRouteError, resolve_worker_route
from app.workers.profiles import WORKER_PROFILES, validate_worker_profile
from app.workers.provider import (
    AnthropicWorkerProvider,
    PythonWorkerProvider,
    WorkerExecutionRequest,
    WorkerProviderError,
    create_worker_provider,
)
from app.workers.registry import build_default_registry


def _enable_hybrid(monkeypatch, *, live=False):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", live)


def _request(worker_id: str, task_type: str, capability: str, payload: dict):
    tier = build_default_registry()[worker_id].model_tier
    return WorkerExecutionRequest(
        task_id="task_harness_0001",
        task_type=task_type,
        required_capability=capability,
        title="Synthetic bounded task",
        input_payload=payload,
        correlation_id="corr_harness_0001",
        attempt_request_id="attempt_harness_0001",
        model_tier=tier,
    )


def test_seven_profiles_match_the_canonical_worker_roster():
    workers = build_default_registry()
    assert set(WORKER_PROFILES) == set(workers)
    assert WORKER_PROFILES["haiku"].max_output_tokens == 700
    assert WORKER_PROFILES["sonnet"].max_output_tokens == 1400
    assert WORKER_PROFILES["opus"].max_output_tokens == 1800
    for worker_id, worker in workers.items():
        validate_worker_profile(worker_id, worker.model_tier)
    assert [worker_id for worker_id, profile in WORKER_PROFILES.items() if profile.execution_mode == "AI"] == [
        "haiku", "sonnet", "opus"
    ]


@pytest.mark.parametrize("worker_id", ["supervisor", "decision_engineer", "market_monitor", "data_archivist"])
def test_python_workers_route_without_paid_provider_even_when_live_gate_is_off(monkeypatch, worker_id):
    _enable_hybrid(monkeypatch, live=False)
    worker = build_default_registry()[worker_id]
    route = resolve_worker_route(worker)
    assert route.route_kind == "PYTHON"
    assert route.model_tier == "NONE"
    assert isinstance(create_worker_provider(worker), PythonWorkerProvider)


def test_ai_worker_is_blocked_until_independent_live_switch_is_enabled(monkeypatch):
    _enable_hybrid(monkeypatch, live=False)
    with pytest.raises(HarnessRouteError) as raised:
        resolve_worker_route(build_default_registry()["haiku"])
    assert raised.value.code == "LIVE_PROVIDER_DISABLED"


def test_ai_worker_routes_to_shared_anthropic_gateway_only_when_explicitly_enabled(monkeypatch):
    _enable_hybrid(monkeypatch, live=True)
    worker = build_default_registry()["haiku"]
    route = resolve_worker_route(worker)
    assert route.route_kind == "ANTHROPIC"
    assert route.model_id == settings.AI_MODEL_FAST
    assert route.max_output_tokens == 700
    assert route.billing_currency == "USD"
    assert isinstance(create_worker_provider(worker), AnthropicWorkerProvider)


def test_legacy_omniroute_mode_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "OMNIROUTE")
    with pytest.raises(HarnessRouteError) as raised:
        resolve_worker_route(build_default_registry()["haiku"])
    assert raised.value.code == "UNKNOWN_PROVIDER_MODE"


def test_decision_engineer_executes_structured_rules_without_ai_or_trading(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["decision_engineer"]
    request = _request(
        "decision_engineer",
        "RULE_VALIDATION",
        "rule_validation",
        {
            "facts": {"spread": 0.18, "tick_fresh": True},
            "checks": [
                {"id": "spread-limit", "path": "spread", "operator": "lte", "expected": 0.20},
                {"id": "freshness", "path": "tick_fresh", "operator": "eq", "expected": True},
            ],
        },
    )
    result = PythonWorkerProvider().execute(worker, request)
    assert result.source == "PYTHON"
    assert result.result_payload["decision"] == "PASS"
    assert result.result_payload["order_sent"] is False
    assert result.usage is None
    assert result.billing_currency is None


def test_decision_engineer_returns_review_when_rule_fails(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["decision_engineer"]
    request = _request(
        "decision_engineer", "RULE_VALIDATION", "rule_validation",
        {"facts": {"spread": 0.31}, "checks": [{"path": "spread", "operator": "lte", "expected": 0.20}]},
    )
    result = PythonWorkerProvider().execute(worker, request)
    assert result.result_payload["decision"] == "REVIEW"
    assert result.result_payload["failed_check_count"] == 1


def test_market_monitor_only_inspects_supplied_snapshot(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["market_monitor"]
    now = datetime.now(timezone.utc).isoformat()
    request = _request(
        "market_monitor", "MARKET_MONITORING_SIMULATION", "market_monitoring_simulation",
        {"market_data": {"symbol": "XAUUSD", "bid": 4000.10, "ask": 4000.18, "observed_at": now, "source": "UNIT_TEST"}},
    )
    result = PythonWorkerProvider().execute(worker, request)
    assert result.source == "PYTHON"
    assert result.result_payload["freshness"] == "FRESH"
    assert result.result_payload["spread"] == pytest.approx(0.08)
    assert result.result_payload["observation_source"] == "UNIT_TEST"
    assert "no live market connection" in result.result_payload["note"]


def test_market_monitor_fails_closed_for_missing_timestamp(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["market_monitor"]
    request = _request(
        "market_monitor", "MARKET_MONITORING_SIMULATION", "market_monitoring_simulation",
        {"market_data": {"symbol": "XAUUSD", "bid": 4000.1, "ask": 4000.2}},
    )
    with pytest.raises(WorkerProviderError) as raised:
        PythonWorkerProvider().execute(worker, request)
    assert raised.value.code == "MARKET_TIMESTAMP_MISSING"


def test_data_archivist_creates_hash_manifest_without_external_write(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["data_archivist"]
    request = _request(
        "data_archivist", "DATA_ARCHIVING", "data_archiving",
        {"records": [{"id": 1, "text": "synthetic"}], "source": "unit"},
    )
    result = PythonWorkerProvider().execute(worker, request)
    assert result.source == "PYTHON"
    assert len(result.result_payload["manifest"]["sha256"]) == 64
    assert result.result_payload["manifest"]["record_count"] == 1
    assert result.result_payload["manifest"]["storage_action"] == "MANIFEST_ONLY"


def test_supervisor_creates_a_routing_plan_but_does_not_enqueue_tasks(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["supervisor"]
    request = _request(
        "supervisor", "TASK_DECOMPOSITION", "task_decomposition",
        {"subtasks": [{"required_capability": "quick_research", "task_type": "QUICK_RESEARCH"}]},
    )
    result = PythonWorkerProvider().execute(worker, request)
    assert result.result_payload["plan"][0]["candidate_worker_ids"] == ["haiku"]
    assert result.result_payload["dispatch_performed"] is False


def test_python_worker_refuses_unimplemented_free_form_task(monkeypatch):
    _enable_hybrid(monkeypatch)
    worker = build_default_registry()["supervisor"]
    request = _request("supervisor", "TASK_DECOMPOSITION", "task_decomposition", {"instruction": "invent a plan"})
    with pytest.raises(WorkerProviderError) as raised:
        PythonWorkerProvider().execute(worker, request)
    assert raised.value.code == "SUPERVISOR_PLAN_SCHEMA_UNSUPPORTED"


def test_python_worker_cannot_be_invoked_outside_hybrid_runtime(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "MOCK")
    worker = build_default_registry()["data_archivist"]
    request = _request("data_archivist", "DATA_ARCHIVING", "data_archiving", {"records": []})
    with pytest.raises(WorkerProviderError) as raised:
        PythonWorkerProvider().execute(worker, request)
    assert raised.value.code == "WORKER_RUNTIME_DISABLED"
