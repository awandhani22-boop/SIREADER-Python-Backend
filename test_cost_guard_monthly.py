from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.ai import cost_guard


class FakeDB:
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


def setup_db(monkeypatch, db):
    monkeypatch.setattr(cost_guard, "SessionLocal", lambda: db)
    monkeypatch.setattr(cost_guard, "_lock_budget", lambda session: None)
    monkeypatch.setattr(cost_guard, "current_usage_date", lambda: date(2026, 11, 3))
    monkeypatch.setattr(
        cost_guard, "_daily_totals",
        lambda session, usage_date: (Decimal("0"), Decimal("0")),
    )


def test_period_start_30_hari_bergulir(monkeypatch):
    monkeypatch.setattr(cost_guard, "current_usage_date", lambda: date(2026, 11, 3))
    assert cost_guard.current_period_start() == date(2026, 10, 5)


def test_reservasi_ditolak_jika_kuota_tier_habis(monkeypatch):
    db = FakeDB()
    setup_db(monkeypatch, db)

    def monthly_totals(session, usage_date, tier=None):
        if tier == "FAST":
            return Decimal("25000"), Decimal("0")
        return Decimal("25000"), Decimal("0")

    monkeypatch.setattr(cost_guard, "_monthly_totals", monthly_totals)

    with pytest.raises(cost_guard.BudgetExceededError, match="tier FAST"):
        cost_guard.reserve_ai_request(
            tier="FAST", prompt_text="x", max_output_tokens=1,
            request_id="monthly-fast-limit",
        )
    assert db.added is None


def test_reservasi_ditolak_jika_kuota_global_habis(monkeypatch):
    db = FakeDB()
    setup_db(monkeypatch, db)

    def monthly_totals(session, usage_date, tier=None):
        if tier is None:
            return Decimal("50000"), Decimal("0")
        return Decimal("0"), Decimal("0")

    monkeypatch.setattr(cost_guard, "_monthly_totals", monthly_totals)

    with pytest.raises(cost_guard.BudgetExceededError, match="global 30 hari"):
        cost_guard.reserve_ai_request(
            tier="REASONING", prompt_text="x", max_output_tokens=1,
            request_id="monthly-global-limit",
        )
    assert db.added is None


def test_kuota_model_lain_tidak_terpakai_saat_fast_habis(monkeypatch):
    db = FakeDB()
    setup_db(monkeypatch, db)

    def monthly_totals(session, usage_date, tier=None):
        if tier is None:
            return Decimal("25000"), Decimal("0")
        if tier == "FAST":
            return Decimal("25000"), Decimal("0")
        return Decimal("0"), Decimal("0")

    monkeypatch.setattr(cost_guard, "_monthly_totals", monthly_totals)

    result = cost_guard.reserve_ai_request(
        tier="REASONING", prompt_text="x", max_output_tokens=1,
        request_id="sonnet-independent-quota",
    )
    assert db.added is not None
    assert db.added.tier == "REASONING"
    assert db.added.input_tokens > 0
    assert db.added.output_tokens == 1
    assert result["tier_budget_rp"] == Decimal("22500")


def test_monthly_usage_melaporkan_global_dan_per_tier(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(cost_guard, "SessionLocal", lambda: db)
    monkeypatch.setattr(cost_guard, "current_usage_date", lambda: date(2026, 11, 3))

    values = {
        None: (Decimal("100"), Decimal("10")),
        "FAST": (Decimal("50"), Decimal("5")),
        "REASONING": (Decimal("40"), Decimal("4")),
        "ESCALATION": (Decimal("10"), Decimal("1")),
    }
    token_values = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 5,
        "cache_write_tokens": 3,
        "total_tokens": 128,
    }

    monkeypatch.setattr(
        cost_guard, "_monthly_totals",
        lambda session, usage_date, tier=None: values[tier],
    )
    monkeypatch.setattr(
        cost_guard, "_monthly_token_totals",
        lambda session, usage_date, tier=None, reserved=False: token_values,
    )

    usage = cost_guard.get_monthly_usage()
    assert usage["period_start"] == "2026-10-05"
    assert usage["period_days"] == 30
    assert usage["projected_cost_rp"] == Decimal("110.000000")
    assert usage["tiers"]["FAST"]["projected_cost_rp"] == Decimal("55.000000")
    assert usage["tiers"]["REASONING"]["budget_rp"] == Decimal("22500")
    assert usage["actual_tokens"]["total_tokens"] == 128
    assert usage["tiers"]["FAST"]["actual_tokens"]["input_tokens"] == 100


def test_harga_tier_mengikuti_tarif_tokenmurah_yang_ditetapkan():
    assert cost_guard.get_tier_config("FAST")["input_price"] == 528
    assert cost_guard.get_tier_config("FAST")["output_price"] == 2640
    assert cost_guard.get_tier_config("REASONING")["input_price"] == 1346
    assert cost_guard.get_tier_config("REASONING")["output_price"] == 6732
    assert cost_guard.get_tier_config("ESCALATION")["input_price"] == 4749
    assert cost_guard.get_tier_config("ESCALATION")["output_price"] == 23747
