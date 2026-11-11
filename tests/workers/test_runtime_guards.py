from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.workers import runtime
from app.workers.provider import WorkerProviderError


def test_run_worker_once_fails_before_database_or_heartbeat_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", False)

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("disabled runtime must not touch DB or write a heartbeat")

    monkeypatch.setattr(runtime, "_record_heartbeat", forbidden_side_effect)

    with pytest.raises(WorkerProviderError) as raised:
        runtime.run_worker_once(
            "haiku",
            "unit-test-instance-0001",
            datetime.now(timezone.utc),
        )

    assert raised.value.code == "WORKER_RUNTIME_DISABLED"


def test_run_worker_forever_fails_before_starting_process_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", False)

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("disabled runtime must not start or heartbeat a worker process")

    monkeypatch.setattr(runtime, "_record_heartbeat", forbidden_side_effect)

    with pytest.raises(WorkerProviderError) as raised:
        runtime.run_worker_forever("haiku")

    assert raised.value.code == "WORKER_RUNTIME_DISABLED"


@pytest.mark.parametrize("interval", [0, 0.09, 30.01, 60, -1])
def test_worker_poll_interval_rejected_before_process_start(monkeypatch, interval: float) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("invalid poll interval must be rejected before process startup")

    monkeypatch.setattr(runtime, "_record_heartbeat", forbidden_side_effect)

    with pytest.raises(ValueError, match="poll_interval_seconds must be between 0.1 and 30 seconds"):
        runtime.run_worker_forever("haiku", poll_interval_seconds=interval)



def test_live_worker_database_identity_is_checked_before_any_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_DATABASE_URL", None)

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("missing worker DB identity must fail before heartbeat or DB access")

    monkeypatch.setattr(runtime, "_record_heartbeat", forbidden_side_effect)

    with pytest.raises(WorkerProviderError) as raised:
        runtime.run_worker_once(
            "haiku",
            "unit-test-instance-live-0001",
            datetime.now(timezone.utc),
        )

    assert raised.value.code == "WORKER_DATABASE_URL_REQUIRED"


def test_live_worker_rejects_core_postgres_identity_before_any_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "WORKER_DATABASE_URL",
        "postgresql+psycopg://postgres.projectref:secret@db.example.test:5432/postgres?sslmode=require",
    )

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("broad postgres login must fail before heartbeat or DB access")

    monkeypatch.setattr(runtime, "_record_heartbeat", forbidden_side_effect)

    with pytest.raises(WorkerProviderError) as raised:
        runtime.run_worker_forever("haiku")

    assert raised.value.code == "WORKER_DATABASE_ROLE_INVALID"


def test_live_worker_requires_worker_database_before_process_start(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", "ANTHROPIC")
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", True)
    monkeypatch.setattr(settings, "WORKER_DATABASE_URL", None)

    def forbidden_side_effect(*_args, **_kwargs):
        raise AssertionError("worker without dedicated DB identity must not start")

    monkeypatch.setattr(runtime, "_record_heartbeat", forbidden_side_effect)

    with pytest.raises(WorkerProviderError) as raised:
        runtime.run_worker_forever("haiku")

    assert raised.value.code == "WORKER_DATABASE_URL_REQUIRED"
