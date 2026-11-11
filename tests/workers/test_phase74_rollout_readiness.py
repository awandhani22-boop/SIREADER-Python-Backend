from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.workers.harness import HarnessRouteError, resolve_worker_route
from app.workers.policy import PROVIDER_FORBIDDEN_WORKERS, PROVIDER_WORKERS
from app.workers.profiles import WORKER_PROFILES
from app.workers.repository import GLOBAL_PROVIDER_REQUEST_LIMIT
from app.workers.registry import build_default_registry, eligible_workers


AI_CASES = (
    ("haiku", "FAST", "AI_MODEL_FAST", 700, "quick_research"),
    ("sonnet", "REASONING", "AI_MODEL_REASONING", 1400, "structured_reasoning"),
    ("opus", "ESCALATION", "AI_MODEL_ESCALATION", 1800, "deep_review"),
)


@pytest.fixture
def live_route_settings(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", True)


@pytest.mark.parametrize(
    "worker_id,tier,model_setting,output_limit,capability",
    AI_CASES,
)
def test_each_general_purpose_ai_worker_has_one_fixed_costed_route(
    live_route_settings,
    worker_id,
    tier,
    model_setting,
    output_limit,
    capability,
):
    registry = build_default_registry()
    route = resolve_worker_route(registry[worker_id])

    assert route.worker_id == worker_id
    assert route.route_kind == "ANTHROPIC"
    assert route.model_tier == tier
    assert route.model_id == getattr(settings, model_setting)
    assert route.max_output_tokens == output_limit
    assert route.billing_currency == "USD"
    assert eligible_workers(capability, registry) == (registry[worker_id],)


@pytest.mark.parametrize(
    "worker_id,tier,model_setting,output_limit,capability",
    AI_CASES,
)
def test_every_ai_worker_fails_closed_when_live_provider_switch_is_off(
    monkeypatch,
    worker_id,
    tier,
    model_setting,
    output_limit,
    capability,
):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", False)

    with pytest.raises(HarnessRouteError) as raised:
        resolve_worker_route(build_default_registry()[worker_id])

    assert raised.value.code == "LIVE_PROVIDER_DISABLED"


@pytest.mark.parametrize(
    "worker_id",
    ("supervisor", "decision_engineer", "market_monitor", "data_archivist"),
)
def test_deterministic_workers_never_receive_an_anthropic_route(
    live_route_settings,
    worker_id,
):
    route = resolve_worker_route(build_default_registry()[worker_id])

    assert route.route_kind == "PYTHON"
    assert route.model_tier == "NONE"
    assert route.model_id is None
    assert route.max_output_tokens == 0
    assert route.billing_currency is None
    assert WORKER_PROFILES[worker_id].ai_enabled_by_default is False


def test_live_provider_allowlist_excludes_deterministic_and_market_workers():
    assert PROVIDER_WORKERS == frozenset({"haiku", "sonnet", "opus"})
    assert PROVIDER_FORBIDDEN_WORKERS == frozenset({"decision_engineer", "market_monitor"})
    assert set(PROVIDER_WORKERS).isdisjoint(PROVIDER_FORBIDDEN_WORKERS)


def test_phase74_has_one_global_live_provider_slot_in_addition_to_worker_limits():
    registry = build_default_registry()

    assert registry["haiku"].max_concurrency == 2
    assert registry["sonnet"].max_concurrency == 1
    assert registry["opus"].max_concurrency == 1
    assert GLOBAL_PROVIDER_REQUEST_LIMIT == 1
    # The database gate serializes all AI worker identities, regardless of
    # the individual concurrency configured in the worker registry.
    assert all(worker.max_concurrency >= 1 for worker in registry.values())


def test_provider_gate_migration_is_local_only_and_does_not_grant_command_api_dispatch():
    backend_root = Path(__file__).resolve().parents[2]
    migration = (backend_root / "migrations" / "2026_11_11_workforce_global_provider_gate_v1.sql").read_text()
    rollback = (backend_root / "migrations" / "2026_11_11_workforce_global_provider_gate_v1_rollback.sql").read_text()

    assert "IF current_database() <> 'sireader_test'" in migration
    assert "SECURITY DEFINER" in migration
    assert "ALTER FUNCTION public.sireader_provider_slot_available_v1(integer)" in migration
    assert "GRANT EXECUTE ON FUNCTION public.sireader_provider_slot_available_v1(integer)" in migration
    assert "TO postgres;" in migration
    assert "FROM PUBLIC, sireader_api_ro;" in migration
    assert "TO sireader_workforce_cmd" not in migration
    assert "IF current_database() <> 'sireader_test'" in rollback
