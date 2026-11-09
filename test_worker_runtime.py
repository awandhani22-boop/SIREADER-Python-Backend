import pytest

from app.config import settings
from app.workers.provider import WorkerProviderError
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
