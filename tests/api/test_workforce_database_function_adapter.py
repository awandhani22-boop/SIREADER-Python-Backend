from __future__ import annotations

import json

from app.command_api.database_commands import (
    adjudicate_review,
    cancel_task,
    command_functions_available,
    enqueue_task,
    get_pending_review,
    review_functions_available,
    retry_task,
    set_worker_scheduling,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeSession:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult(self.values.pop(0))


def test_command_function_probe_requires_complete_entry_surface():
    db = FakeSession([4])
    assert command_functions_available(db) is True
    sql, _ = db.calls[0]
    assert "command_enqueue_task_v1" in sql
    assert "command_set_worker_scheduling_v1" in sql


def test_review_function_probe_requires_read_and_adjudication_functions():
    db = FakeSession([2])
    assert review_functions_available(db) is True
    sql, _ = db.calls[0]
    assert "review_get_pending_v1" in sql
    assert "review_adjudicate_v1" in sql


def test_enqueue_uses_fixed_database_function_and_bounded_json_parameter():
    expected = {"command_id": "id1", "outcome": {"task_id": "task1"}}
    db = FakeSession([expected])
    result = enqueue_task(
        db,
        task_type="QUICK_RESEARCH",
        title="Synthetic research",
        required_capability="quick_research",
        requested_worker_id="haiku",
        input_payload={"source": "SIMULATED", "text": "safe"},
        priority=50,
        max_attempts=2,
        idempotency_key="test-enqueue-0001",
    )
    sql, params = db.calls[0]
    assert result == expected
    assert "command_enqueue_task_v1" in sql
    assert "CAST(:input_payload AS jsonb)" in sql
    assert json.loads(params["input_payload"])["source"] == "SIMULATED"
    assert params["requested_worker_id"] == "haiku"


def test_cancel_retry_and_worker_scheduling_use_only_fixed_functions():
    db = FakeSession([
        {"outcome": {"status": "CANCELLED"}},
        {"outcome": {"status": "QUEUED", "task_id": "new-task"}},
        {"outcome": {"schedule_state": "PAUSED"}},
    ])
    assert cancel_task(db, task_id="task-12345678", idempotency_key="cancel-0001")["outcome"]["status"] == "CANCELLED"
    assert retry_task(db, task_id="task-12345678", idempotency_key="retry-0001")["outcome"]["status"] == "QUEUED"
    assert set_worker_scheduling(
        db, worker_id="haiku", accepting_tasks=False, idempotency_key="pause-0001"
    )["outcome"]["schedule_state"] == "PAUSED"
    assert "command_cancel_task_v1" in db.calls[0][0]
    assert "command_retry_task_v1" in db.calls[1][0]
    assert "command_set_worker_scheduling_v1" in db.calls[2][0]


def test_pending_review_is_read_through_trusted_database_function():
    expected = {"review_id": "rev_test_00000001", "status": "PENDING"}
    db = FakeSession([expected])
    result = get_pending_review(db, review_id="rev_test_00000001")
    assert result == expected
    sql, params = db.calls[0]
    assert "review_get_pending_v1" in sql
    assert params["review_id"] == "rev_test_00000001"


def test_review_adjudication_does_not_accept_an_actor_id_parameter():
    expected = {"outcome": {"decision": "APPROVED", "adjudicated_by": "local_operator"}}
    db = FakeSession([expected])
    adjusted = {"summary": "approved edit", "api_token": "[REDACTED]"}
    result = adjudicate_review(
        db,
        review_id="rev_test_00000001",
        decision="EDITED",
        expected_original_sha256="a" * 64,
        adjusted_output=adjusted,
        reason_code=None,
        notes="sanitized edit",
        idempotency_key="review-edit-0001",
    )
    sql, params = db.calls[0]
    assert result == expected
    assert "review_adjudicate_v1" in sql
    assert "operator_id" not in params and "actor_id" not in params
    assert json.loads(params["adjusted_output"]) == adjusted


import pytest


@pytest.mark.parametrize(
    ("probe", "present", "message"),
    [
        (command_functions_available, 2, "command database function layer"),
        (review_functions_available, 1, "review database function layer"),
    ],
)
def test_partial_function_layer_fails_closed(probe, present, message):
    db = FakeSession([present])
    with pytest.raises(RuntimeError, match=message):
        probe(db)
