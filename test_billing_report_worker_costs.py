from decimal import Decimal

from app.ai import billing_report


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement):
        return FakeResult(self.rows)


def test_worker_report_joins_ai_usage_to_worker_task_attempts(monkeypatch):
    rows = [
        ("haiku", "task-0001", 2, Decimal("0.001"), Decimal("0.0002"), 200, 80, 20, 5),
        ("haiku", "task-0002", 1, Decimal("0.002"), Decimal("0"), 100, 30, 0, 0),
        ("sonnet", "task-0003", 1, Decimal("0.010"), Decimal("0.001"), 500, 120, 0, 0),
        (None, None, 1, Decimal("0.003"), Decimal("0"), 70, 15, 0, 0),
    ]
    monkeypatch.setattr(
        billing_report, "SessionLocal", lambda: FakeSession(rows)
    )
    monkeypatch.setattr(
        billing_report, "current_usage_date", lambda: __import__("datetime").date(2026, 11, 10)
    )

    report = billing_report.get_worker_report("USD", period_days=30)
    by_worker = {row["worker_id"]: row for row in report["workers"]}

    assert by_worker["haiku"]["requests"] == 3
    assert by_worker["haiku"]["task_count"] == 2
    assert by_worker["haiku"]["actual_cost"] == Decimal("0.003000")
    assert by_worker["haiku"]["reserved_cost"] == Decimal("0.000200")
    assert by_worker["haiku"]["tokens"]["input"] == 300

    assert by_worker["sonnet"]["requests"] == 1
    assert by_worker["sonnet"]["actual_cost"] == Decimal("0.010000")

    # Trading pipeline or other non-worker model requests remain visible and
    # are not incorrectly assigned to one of the virtual-office workers.
    assert by_worker[None]["actual_cost"] == Decimal("0.003000")
    assert len(report["tasks"]) == 4
