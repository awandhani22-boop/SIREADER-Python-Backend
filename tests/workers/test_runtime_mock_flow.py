from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.workers import runtime
from app.workers.provider import MockWorkerProvider
from app.workers.registry import build_default_registry


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return self


def configure_fake_runtime(monkeypatch, task):
    now = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    attempt = SimpleNamespace(attempt_id="attempt_00000001", lease_token="unit-test-lease-token")
    effects = {"claims": [], "starts": [], "completions": [], "failures": [], "heartbeats": [], "process_heartbeats": [], "provider_factory": []}
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    # The legacy session fallback is permitted only for an explicit loopback
    # disposable/test database; this suite replaces all DB access with FakeSession.
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgresql+psycopg://unit_test@127.0.0.1:5432/sireader_unit_test_unused",
    )
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "MOCK")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", False)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)
    monkeypatch.setattr(runtime, "SessionLocal", FakeSession)
    monkeypatch.setattr(runtime, "_record_heartbeat", lambda *args, **kwargs: effects["process_heartbeats"].append((args, kwargs)))
    monkeypatch.setattr(runtime, "_load_worker_contract", lambda _db, worker_id: build_default_registry()[worker_id])

    def claim(_db, **kwargs):
        effects["claims"].append(kwargs)
        return None if task is None else (task, attempt)

    monkeypatch.setattr(runtime, "claim_next_task", claim)
    monkeypatch.setattr(runtime, "start_task", lambda _db, **kwargs: effects["starts"].append(kwargs))
    monkeypatch.setattr(runtime, "heartbeat_worker", lambda _db, contract: effects["heartbeats"].append(contract))
    monkeypatch.setattr(runtime, "complete_task", lambda _db, **kwargs: effects["completions"].append(kwargs))
    monkeypatch.setattr(runtime, "fail_task", lambda _db, **kwargs: effects["failures"].append(kwargs))
    monkeypatch.setattr(runtime, "create_worker_provider", lambda worker: effects["provider_factory"].append(worker.worker_id) or MockWorkerProvider())

    def forbidden_provider_reservation(*_args, **_kwargs):
        raise AssertionError("MOCK execution must never create a provider reservation")

    monkeypatch.setattr(runtime, "mark_provider_requested", forbidden_provider_reservation)
    return effects, now


def make_task(**overrides):
    values = {
        "task_id": "task_00000001",
        "attempt_id": "attempt_00000001",
        "lease_token": "unit-test-lease-token",
        "task_type": "QUICK_RESEARCH",
        "required_capability": "quick_research",
        "title": "Summarize synthetic text",
        "input_payload": {"text": "This is a harmless synthetic paragraph for a mock worker test."},
        "correlation_id": "corr_00000001",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_worker_runtime_does_not_import_or_run_maintenance_operations() -> None:
    # Maintenance is a separate trust boundary and must never be reachable
    # through the ordinary worker runtime identity.
    forbidden = (
        "recover_expired_leases",
        "expire_deadline_tasks",
        "expire_stale_reviews",
        "purge_expired_task_payloads",
        "purge_expired_review_payloads",
    )
    assert all(not hasattr(runtime, name) for name in forbidden)


def test_run_worker_once_completes_one_task_with_real_mock_provider_and_no_billing(monkeypatch) -> None:
    effects, now = configure_fake_runtime(monkeypatch, make_task())

    worked = runtime.run_worker_once(
        "haiku",
        "unit-test-instance-0001",
        now,
    )

    assert worked is True
    assert len(effects["claims"]) == 1
    assert len(effects["starts"]) == 1
    assert len(effects["completions"]) == 1
    assert effects["failures"] == []
    assert effects["provider_factory"] == ["haiku"]
    assert len(effects["process_heartbeats"]) == 2

    completion = effects["completions"][0]
    assert completion["task_id"] == "task_00000001"
    assert completion["lease_token"] == "unit-test-lease-token"
    assert completion["result_payload"]["source"] == "SIMULATED"
    assert completion["result_payload"]["worker_id"] == "haiku"
    assert completion["billing_currency"] is None
    assert completion["cost_amount"] is None
    assert completion["force_human_review"] is False


def test_run_worker_once_does_not_build_provider_when_queue_is_empty(monkeypatch) -> None:
    effects, now = configure_fake_runtime(monkeypatch, None)

    worked = runtime.run_worker_once(
        "haiku",
        "unit-test-instance-0001",
        now,
    )

    assert worked is False
    assert len(effects["claims"]) == 1
    assert effects["provider_factory"] == []
    assert effects["starts"] == []
    assert effects["completions"] == []
    assert effects["failures"] == []


def test_run_worker_once_rejects_trade_instruction_before_provider_creation(monkeypatch) -> None:
    task = make_task(title="Place a live trade now")
    effects, now = configure_fake_runtime(monkeypatch, task)

    worked = runtime.run_worker_once(
        "haiku",
        "unit-test-instance-0001",
        now,
    )

    assert worked is True
    assert effects["provider_factory"] == []
    assert effects["starts"] == []
    assert effects["completions"] == []
    assert len(effects["failures"]) == 1
    assert effects["failures"][0]["error_code"] == "SENSITIVE_TASK_REJECTED"
    assert effects["failures"][0]["outcome_unknown"] is False
