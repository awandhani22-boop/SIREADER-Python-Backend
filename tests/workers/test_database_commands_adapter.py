from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.workers.database_commands import (
    claim_next_task,
    complete_task,
    fail_task,
    heartbeat_worker,
    mark_provider_requested,
    renew_task_lease,
    start_task,
    worker_get_contract,
)
from app.workers.repository import GlobalProviderConcurrencyLimitError
from app.workers.runtime_contract import WorkerHeartbeatContract, WorkerProcessState


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult(self.result)


def test_worker_contract_uses_authenticated_database_function():
    payload = {
        "schema_version": 1,
        "worker_id": "haiku",
        "display_name": "Haiku Worker",
        "role": "Research",
        "department": "Research",
        "capability_ids": ["quick_research", "summarization"],
        "enabled": True,
        "max_concurrency": 1,
        "model_tier": "FAST",
    }
    db = FakeSession(payload)

    result = worker_get_contract(db, worker_id="haiku")

    assert result.worker_id == "haiku"
    assert result.capability_ids == payload["capability_ids"]
    assert "worker_get_contract_v1" in db.calls[0][0]
    assert db.calls[0][1] == {"worker_id": "haiku"}


def test_heartbeat_uses_only_database_command_function():
    now = datetime.now(timezone.utc)
    contract = WorkerHeartbeatContract(
        worker_id="haiku",
        instance_id="instance-haiku-0001",
        process_state=WorkerProcessState.READY,
        process_started_at=now,
        heartbeat_at=now,
        active_task_count=0,
        in_flight_provider_requests=0,
        observed_at=now,
    )
    db = FakeSession({"worker_id": "haiku", "instance_id": contract.instance_id})

    result = heartbeat_worker(db, contract)

    assert result.worker_id == "haiku"
    assert "worker_heartbeat_v1" in db.calls[0][0]
    assert db.calls[0][1]["process_state"] == "READY"


def test_claim_returns_task_and_attempt_namespaces():
    db = FakeSession({
        "task": {
            "task_id": "task-12345678",
            "task_type": "QUICK_RESEARCH",
            "required_capability": "quick_research",
            "title": "Synthetic task",
            "input_payload": {"source": "SIMULATED"},
            "correlation_id": "corr-12345678",
        },
        "attempt": {
            "attempt_id": "attempt-12345678",
            "task_id": "task-12345678",
            "lease_token": "lease-12345678",
            "worker_id": "haiku",
        },
    })

    claimed = claim_next_task(
        db,
        worker_id="haiku",
        instance_id="instance-haiku-0001",
        lease_seconds=45,
        heartbeat_timeout_seconds=60,
    )

    assert claimed[0].task_id == "task-12345678"
    assert claimed[1].lease_token == "lease-12345678"
    assert "worker_claim_next_v1" in db.calls[0][0]
    assert db.calls[0][1]["lease_seconds"] == 45


def test_start_and_renew_pass_authenticated_worker_instance():
    db = FakeSession({"task_id": "task-12345678", "status": "RUNNING"})

    start_task(
        db,
        task_id="task-12345678",
        lease_token="lease-12345678",
        worker_id="haiku",
        instance_id="instance-haiku-0001",
    )
    renew_task_lease(
        db,
        task_id="task-12345678",
        lease_token="lease-12345678",
        worker_id="haiku",
        instance_id="instance-haiku-0001",
        lease_seconds=60,
    )

    assert "worker_start_v1" in db.calls[0][0]
    assert "worker_renew_lease_v1" in db.calls[1][0]
    assert db.calls[0][1]["worker_id"] == "haiku"
    assert db.calls[1][1]["instance_id"] == "instance-haiku-0001"


def test_provider_reservation_maps_busy_slot_to_domain_error():
    db = FakeSession({"acquired": False, "attempt_id": "attempt-12345678"})

    with pytest.raises(GlobalProviderConcurrencyLimitError):
        mark_provider_requested(
            db,
            task_id="task-12345678",
            lease_token="lease-12345678",
            worker_id="haiku",
            instance_id="instance-haiku-0001",
            provider_request_id="provider-request-123",
        )

    assert "worker_reserve_provider_slot_v1" in db.calls[0][0]


def test_completion_uses_typed_json_and_numeric_parameters():
    db = FakeSession({
        "task_id": "task-12345678",
        "status": "COMPLETED",
        "requires_review": False,
        "review_id": None,
    })
    payload = {"summary": "synthetic", "source": "SIMULATED"}
    context = {
        "policy_version": "2026.11.10-harness.1",
        "sensitive_action": "ALLOW",
        "model_tier": "FAST",
        "reason_code": "POLICY_ALLOWED",
        "redacted_fields": 0,
    }

    result = complete_task(
        db,
        task_id="task-12345678",
        lease_token="lease-12345678",
        worker_id="haiku",
        instance_id="instance-haiku-0001",
        result_payload=payload,
        billing_currency="IDR",
        cost_amount="0.001",
        policy_context=context,
    )

    statement, params = db.calls[0]
    assert result.status == "COMPLETED"
    assert "worker_complete_task_v1" in statement
    assert "CAST(:result_payload AS jsonb)" in statement
    assert "CAST(:policy_context AS jsonb)" in statement
    assert "CAST(:cost_amount AS numeric)" in statement
    assert json.loads(params["result_payload"]) == payload
    assert json.loads(params["policy_context"]) == context
    assert params["cost_amount"] == "0.001"


def test_failure_maps_to_database_command():
    db = FakeSession({
        "task_id": "task-12345678",
        "status": "WAITING_REVIEW",
        "failure_code": "PROVIDER_TIMEOUT",
    })

    result = fail_task(
        db,
        task_id="task-12345678",
        lease_token="lease-12345678",
        worker_id="haiku",
        instance_id="instance-haiku-0001",
        error_code="PROVIDER_TIMEOUT",
        retryable=False,
        outcome_unknown=True,
    )

    assert result.status == "WAITING_REVIEW"
    assert "worker_fail_task_v1" in db.calls[0][0]
    assert db.calls[0][1]["outcome_unknown"] is True
