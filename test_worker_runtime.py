from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.workers.provider import WorkerProviderError, WorkerProviderResult
from app.workers import runtime
from app.workers.runtime import run_worker_forever, run_worker_once


def test_worker_process_refuses_to_start_when_runtime_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", False)
    with pytest.raises(WorkerProviderError) as error:
        run_worker_once("haiku", "instance-test-123", __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert error.value.code == "WORKER_RUNTIME_DISABLED"


def test_worker_forever_checks_feature_flag_before_opening_database(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", False)
    with pytest.raises(WorkerProviderError) as error:
        run_worker_forever("haiku")
    assert error.value.code == "WORKER_RUNTIME_DISABLED"


def test_worker_runtime_rejects_unsafe_poll_intervals(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        run_worker_forever("haiku", poll_interval_seconds=0)

def test_worker_publishes_active_and_provider_in_flight_heartbeats_without_waiting(monkeypatch):
    monkeypatch.setattr(settings, "WORKER_RUNTIME_ENABLED", True)
    monkeypatch.setattr(runtime, "_last_maintenance_at", datetime.now(timezone.utc))

    heartbeats = []
    events = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin(self):
            return nullcontext(self)

    class FakeOmniRouteProvider(runtime.OmniRouteWorkerProvider):
        def execute(self, _worker, request):
            assert request.on_provider_reserved is not None
            request.on_provider_reserved("provider-request-12345678")
            return WorkerProviderResult(
                result_payload={"summary": "completed"},
                source="LIVE",
                provider_request_id="provider-request-12345678",
                billing_currency="IDR",
                cost_amount="0.001",
                usage={"input_tokens": 10, "output_tokens": 5},
            )

    task = SimpleNamespace(
        task_id="task-12345678",
        task_type="SUMMARIZATION",
        title="Test task",
        input_payload={"text": "fixture"},
        correlation_id="corr-12345678",
    )
    attempt = SimpleNamespace(attempt_id="attempt-12345678", lease_token="lease-12345678")

    monkeypatch.setattr(runtime, "SessionLocal", FakeSession)
    monkeypatch.setattr(runtime, "claim_next_task", lambda *_args, **_kwargs: (task, attempt))
    monkeypatch.setattr(runtime, "_load_worker_contract", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "create_worker_provider", lambda _worker: FakeOmniRouteProvider())
    monkeypatch.setattr(runtime, "start_task", lambda *_args, **_kwargs: events.append("task-started"))
    monkeypatch.setattr(runtime, "mark_provider_requested", lambda *_args, **_kwargs: events.append("provider-requested"))
    monkeypatch.setattr(runtime, "complete_task", lambda *_args, **_kwargs: events.append("task-completed"))

    def capture_heartbeat(_db, heartbeat):
        heartbeats.append(heartbeat)
        events.append(
            f"heartbeat:{heartbeat.active_task_count}:{heartbeat.in_flight_provider_requests}"
        )

    monkeypatch.setattr(runtime, "heartbeat_worker", capture_heartbeat)

    assert run_worker_once(
        "haiku",
        "instance-test-12345678",
        datetime.now(timezone.utc),
    ) is True

    heartbeat_counts = [
        (item.active_task_count, item.in_flight_provider_requests)
        for item in heartbeats
    ]
    assert (1, 0) in heartbeat_counts
    assert (1, 1) in heartbeat_counts
    assert heartbeat_counts[-1] == (0, 0)

    task_started = events.index("task-started")
    active_heartbeat = events.index("heartbeat:1:0")
    provider_requested = events.index("provider-requested")
    provider_heartbeat = events.index("heartbeat:1:1")
    assert task_started < active_heartbeat < provider_requested < provider_heartbeat

