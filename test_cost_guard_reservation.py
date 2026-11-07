
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.ai import cost_guard


class FakeDB:
    """Database tiruan. Tidak terhubung ke Supabase."""

    def __init__(self, scalar_result=None):
        self.scalar_result = scalar_result
        self.added = None
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def scalar(self, statement):
        return self.scalar_result

    def execute(self, statement, params=None):
        return None

    def add(self, obj):
        self.added = obj

    def commit(self):
        self.committed = True


def fake_log(status="RESERVED"):
    return SimpleNamespace(
        request_id="test-request",
        tier="FAST",
        status=status,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_rp=Decimal("0"),
        reserved_rp=Decimal("10"),
        error_message=None,
        completed_at=None,
    )


def setup_fake_db(monkeypatch, db):
    monkeypatch.setattr(
        cost_guard,
        "SessionLocal",
        lambda: db,
    )
    monkeypatch.setattr(
        cost_guard,
        "_lock_budget",
        lambda session: None,
    )


def test_reservasi_berhasil(monkeypatch):
    db = FakeDB()

    setup_fake_db(monkeypatch, db)

    monkeypatch.setattr(
        cost_guard,
        "_daily_totals",
        lambda session, usage_date: (
            Decimal("0"),
            Decimal("0"),
        ),
    )

    monkeypatch.setattr(
        cost_guard,
        "current_usage_date",
        lambda: date(2026, 11, 3),
    )

    result = cost_guard.reserve_ai_request(
        tier="FAST",
        prompt_text="test request",
        max_output_tokens=10,
        request_id="test-reserve-1",
    )

    assert db.added is not None
    assert db.added.status == "RESERVED"
    assert db.added.reserved_rp > 0
    assert db.committed is True

    assert result["request_id"] == "test-reserve-1"
    assert result["reserved_cost_rp"] > 0


def test_request_duplikat_ditolak(monkeypatch):
    existing = fake_log()
    db = FakeDB(scalar_result=existing)

    setup_fake_db(monkeypatch, db)

    with pytest.raises(cost_guard.DuplicateRequestError):
        cost_guard.reserve_ai_request(
            tier="FAST",
            prompt_text="test",
            max_output_tokens=10,
            request_id="duplicate-request",
        )

    assert db.added is None


def test_reservasi_ditolak_jika_melewati_hard_limit(monkeypatch):
    db = FakeDB()

    setup_fake_db(monkeypatch, db)

    monkeypatch.setattr(
        cost_guard,
        "_daily_totals",
        lambda session, usage_date: (
            Decimal("50000"),
            Decimal("0"),
        ),
    )

    monkeypatch.setattr(
        cost_guard,
        "current_usage_date",
        lambda: date(2026, 11, 3),
    )

    with pytest.raises(cost_guard.BudgetExceededError):
        cost_guard.reserve_ai_request(
            tier="FAST",
            prompt_text="test",
            max_output_tokens=10,
            request_id="over-limit-request",
        )

    assert db.added is None


def test_reservasi_diganti_biaya_aktual(monkeypatch):
    log = fake_log()
    db = FakeDB(scalar_result=log)

    setup_fake_db(monkeypatch, db)

    cost = cost_guard.finalize_ai_request(
        request_id="test-request",
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=200,
        cache_write_tokens=50,
    )

    assert cost == Decimal("0.835600")
    assert log.status == "COMPLETED"
    assert log.cost_rp == Decimal("0.835600")
    assert log.reserved_rp == Decimal("0")
    assert log.input_tokens == 1000
    assert log.output_tokens == 100
    assert db.committed is True


def test_finalize_completed_idempotent(monkeypatch):
    log = fake_log(status="COMPLETED")
    log.cost_rp = Decimal("1.250000")

    db = FakeDB(scalar_result=log)
    setup_fake_db(monkeypatch, db)

    cost = cost_guard.finalize_ai_request(
        request_id="test-request",
        input_tokens=1000,
        output_tokens=100,
    )

    assert cost == Decimal("1.250000")
    assert log.cost_rp == Decimal("1.250000")


def test_status_unknown_mempertahankan_reservasi(monkeypatch):
    log = fake_log()
    db = FakeDB(scalar_result=log)

    setup_fake_db(monkeypatch, db)

    status = cost_guard.mark_ai_request_failed(
        request_id="test-request",
        error_message="Connection timeout",
        definitely_not_charged=False,
    )

    assert status == "UNKNOWN"
    assert log.status == "UNKNOWN"
    assert log.reserved_rp == Decimal("10")
    assert db.committed is True


def test_gagal_dan_pasti_tidak_ditagih_melepas_reservasi(
    monkeypatch,
):
    log = fake_log()
    db = FakeDB(scalar_result=log)

    setup_fake_db(monkeypatch, db)

    status = cost_guard.mark_ai_request_failed(
        request_id="test-request",
        error_message="Request never sent",
        definitely_not_charged=True,
    )

    assert status == "FAILED"
    assert log.status == "FAILED"
    assert log.reserved_rp == Decimal("0")
    assert db.committed is True
