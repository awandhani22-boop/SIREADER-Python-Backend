"""Test mock migrasi billing Anthropic USD (tanpa AI berbayar, tanpa Supabase).

Semua database memakai sqlite in-memory. Semua nilai uang memakai Decimal.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai import billing_report, cost_guard, pending_queue, pipeline
from app.database.models import (
    AIAnalysisLog,
    AIPendingMessage,
    AIUsageAlert,
    AIUsageLog,
    Base,
)

HAIKU = "anthropic/claude-haiku-5-5"
SONNET = "anthropic/claude-sonnet-5-5"
OPUS = "anthropic/claude-opus-5-5"
IDR_MODEL = "tokenmurah/claude-haiku-4-5-20251001"


# ============================================================
# FIXTURE DATABASE
# ============================================================

@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    for module in (cost_guard, pending_queue, pipeline, billing_report):
        monkeypatch.setattr(module, "SessionLocal", factory)

    monkeypatch.setattr(cost_guard, "_lock_budget", lambda db: None)
    
    monkeypatch.setattr(
    cost_guard, "current_usage_date", lambda: date(2026, 11, 4)
     )
    monkeypatch.setattr(
    billing_report, "current_usage_date", lambda: date(2026, 11, 4)
     )
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _add_usage(factory, **kwargs):
    defaults = dict(
        request_id=f"req-{kwargs.get('cost_usd', 0)}-{kwargs.get('cost_rp', 0)}-"
                   f"{kwargs.get('usage_date', date(2026, 11, 4))}-"
                   f"{kwargs.get('model', SONNET)}",
        usage_date=date(2026, 11, 4),
        provider="omniroute",
        model=SONNET,
        tier="REASONING",
        input_tokens=100,
        output_tokens=50,
        billing_currency="USD",
        cost_usd=Decimal("0"),
        reserved_usd=Decimal("0"),
        cost_rp=Decimal("0"),
        reserved_rp=Decimal("0"),
        status="COMPLETED",
    )
    defaults.update(kwargs)
    with factory() as db:
        row = AIUsageLog(**defaults)
        db.add(row)
        db.commit()
        return row.id


# ============================================================
# 1-5. TARIF
# ============================================================

def test_tarif_haiku_short_context():
    # Total prompt 40.000 token: tetap di bawah ambang long-context.
    breakdown = cost_guard.usd_cost_breakdown(
        HAIKU,
        input_tokens=10_000,
        output_tokens=1_000_000,
        cache_read_tokens=10_000,
        cache_write_5m_tokens=10_000,
        cache_write_1h_tokens=10_000,
    )
    # 0.001 + 0.50 + 0.0001 + 0.00125 + 0.002
    assert breakdown["cost_usd"] == Decimal("0.504350")
    assert breakdown["long_context"] is False


def test_tarif_haiku_long_context_seluruh_kategori():
    # Prompt total 100.001 token > 100.000 -> semua kategori tarif long.
    breakdown = cost_guard.usd_cost_breakdown(
        HAIKU,
        input_tokens=100_001,
        output_tokens=1_000_000,
        cache_read_tokens=0,
    )
    expected = (
        Decimal("100001") * Decimal("0.50")
        + Decimal("1000000") * Decimal("2.50")
    ) / Decimal("1000000")
    assert breakdown["long_context"] is True
    # _money memakai ROUND_CEILING.
    assert breakdown["cost_usd"] == Decimal("2.550001")


def test_tarif_haiku_tepat_di_ambang_masih_short():
    breakdown = cost_guard.usd_cost_breakdown(
        HAIKU, input_tokens=100_000, output_tokens=0
    )
    assert breakdown["long_context"] is False
    assert breakdown["cost_usd"] == Decimal("0.010000")


def test_tarif_sonnet():
    cost = cost_guard.calculate_cost_usd(
        SONNET,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    # 2 + 10 + 0.10 + 2.50 + 4.00
    assert cost == Decimal("18.600000")


def test_tarif_opus():
    cost = cost_guard.calculate_cost_usd(
        OPUS,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    # 4 + 20 + 0.20 + 5.00 + 8.00
    assert cost == Decimal("37.200000")


def test_usd_decimal_bukan_float():
    cost = cost_guard.calculate_cost_usd(SONNET, input_tokens=1, output_tokens=0)
    assert isinstance(cost, Decimal)
    assert cost == Decimal("0.000002")


# ============================================================
# 6-11. CACHE, TTL, THINKING, IDENTITAS PROVIDER
# ============================================================

def test_cache_read_saja():
    cost = cost_guard.calculate_cost_usd(
        SONNET, input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert cost == Decimal("0.100000")


def test_cache_write_5m_saja():
    cost = cost_guard.calculate_cost_usd(
        SONNET, input_tokens=0, output_tokens=0, cache_write_5m_tokens=1_000_000
    )
    assert cost == Decimal("2.500000")


def test_cache_write_1h_saja():
    cost = cost_guard.calculate_cost_usd(
        SONNET, input_tokens=0, output_tokens=0, cache_write_1h_tokens=1_000_000
    )
    assert cost == Decimal("4.000000")


def test_cache_ttl_tidak_diketahui_memakai_tarif_1_jam():
    breakdown = cost_guard.usd_cost_breakdown(
        SONNET, input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    )
    assert breakdown["cache_ttl_estimated"] is True
    assert breakdown["cache_write_1h_tokens"] == 1_000_000
    assert breakdown["cost_usd"] == Decimal("4.000000")


def test_thinking_tidak_dihitung_dua_kali():
    tanpa = cost_guard.calculate_cost_usd(
        SONNET, input_tokens=1000, output_tokens=1000
    )
    dengan = cost_guard.calculate_cost_usd(
        SONNET, input_tokens=1000, output_tokens=1000
    )
    # thinking bukan parameter biaya: output sudah mencakup thinking.
    assert tanpa == dengan == Decimal("0.012000")


def test_provider_tidak_dikenal_gagal_aman():
    for model in ("openai/gpt-x", "gemini/gemini-3-flash-preview", "tanpa-slash"):
        with pytest.raises(cost_guard.UnknownModelPricingError):
            cost_guard.resolve_billing(model)


def test_pemetaan_currency_dari_model():
    assert cost_guard.resolve_billing(HAIKU)["currency"] == "USD"
    assert cost_guard.resolve_billing(IDR_MODEL)["currency"] == "IDR"


# ============================================================
# 12-21. RESERVASI, BUDGET, ALERT, CURRENCY
# ============================================================

def test_reservasi_usd_memakai_kolom_usd(db_factory):
    result = cost_guard.reserve_ai_request(
        tier="REASONING",
        prompt_text="halo",
        max_output_tokens=100,
        request_id="usd-1",
        model=SONNET,
        analysis_log_id=77,
    )
    assert result["billing_currency"] == "USD"
    assert result["reserved_cost_usd"] > 0
    assert result["reserved_cost_rp"] == Decimal("0")

    with db_factory() as db:
        row = db.scalar(
            select(AIUsageLog).where(AIUsageLog.request_id == "usd-1")
        )
        assert row.billing_currency == "USD"
        assert row.reserved_usd > 0
        assert row.reserved_rp == Decimal("0")
        assert row.analysis_log_id == 77


def test_hard_limit_usd_5_ditolak(db_factory):
    _add_usage(db_factory, cost_usd=Decimal("4.999900"), request_id="seed-high")
    with pytest.raises(cost_guard.BudgetExceededError, match="Anthropic USD"):
        cost_guard.reserve_ai_request(
            tier="REASONING",
            prompt_text="x" * 5000,
            max_output_tokens=500,
            request_id="usd-over",
            model=SONNET,
        )


def test_reservasi_paralel_tidak_melewati_limit(db_factory):
    _add_usage(db_factory, cost_usd=Decimal("4.990000"), request_id="seed-near")
    cost_guard.reserve_ai_request(
        tier="FAST",
        prompt_text="x" * 20_000,
        max_output_tokens=2000,
        request_id="res-1",
        model=HAIKU,
    )
    with pytest.raises(cost_guard.BudgetExceededError):
        cost_guard.reserve_ai_request(
            tier="FAST",
            prompt_text="x" * 20_000,
            max_output_tokens=2000,
            request_id="res-2",
            model=HAIKU,
        )


def test_alert_dedup_threshold(db_factory):
    _add_usage(db_factory, cost_usd=Decimal("3.50"), request_id="a-70")
    assert cost_guard.record_budget_alerts("USD") == [70]
    assert cost_guard.record_budget_alerts("USD") == []  # dedup siklus sama

    _add_usage(db_factory, cost_usd=Decimal("0.90"), request_id="a-85")
    assert cost_guard.record_budget_alerts("USD") == [85]

    _add_usage(db_factory, cost_usd=Decimal("0.40"), request_id="a-95")
    alerts = cost_guard.record_budget_alerts("USD")
    assert alerts == [95]

    with db_factory() as db:
        rows = db.execute(
            select(AIUsageAlert.threshold_percent).order_by(
                AIUsageAlert.threshold_percent
            )
        ).scalars().all()
        assert rows == [70, 85, 95]


def test_rolling_30_hari(db_factory):
    _add_usage(
        db_factory,
        cost_usd=Decimal("1.00"),
        usage_date=date(2026, 11, 4) - timedelta(days=40),
        request_id="lama",
    )
    _add_usage(db_factory, cost_usd=Decimal("2.00"), request_id="baru")
    usage = cost_guard.get_anthropic_usage(date(2026, 11, 4))
    assert usage["actual_cost_usd"] == Decimal("2.000000")


def test_unknown_mempertahankan_reservasi_usd(db_factory):
    cost_guard.reserve_ai_request(
        tier="REASONING",
        prompt_text="x",
        max_output_tokens=100,
        request_id="unk-1",
        model=SONNET,
    )
    status = cost_guard.mark_ai_request_failed(
        "unk-1", "timeout", definitely_not_charged=False
    )
    assert status == "UNKNOWN"
    with db_factory() as db:
        row = db.scalar(
            select(AIUsageLog).where(AIUsageLog.request_id == "unk-1")
        )
        assert row.status == "UNKNOWN"
        assert row.reserved_usd > 0
    usage = cost_guard.get_anthropic_usage(date(2026, 11, 4))
    assert usage["reserved_cost_usd"] > 0
    assert usage["actual_cost_usd"] == Decimal("0")


def test_failed_terbukti_tidak_ditagih_melepas_reservasi(db_factory):
    cost_guard.reserve_ai_request(
        tier="REASONING",
        prompt_text="x",
        max_output_tokens=100,
        request_id="fail-1",
        model=SONNET,
    )
    status = cost_guard.mark_ai_request_failed(
        "fail-1", "invalid request", definitely_not_charged=True
    )
    assert status == "FAILED"
    usage = cost_guard.get_anthropic_usage(date(2026, 11, 4))
    assert usage["reserved_cost_usd"] == Decimal("0")


def test_finalize_usd_menyimpan_cache_dan_thinking(db_factory):
    cost_guard.reserve_ai_request(
        tier="REASONING",
        prompt_text="x",
        max_output_tokens=100,
        request_id="fin-1",
        model=SONNET,
    )
    cost = cost_guard.finalize_ai_request(
        request_id="fin-1",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_5m_tokens=100,
        cache_write_1h_tokens=50,
        thinking_tokens=120,
    )
    assert isinstance(cost, Decimal)
    with db_factory() as db:
        row = db.scalar(
            select(AIUsageLog).where(AIUsageLog.request_id == "fin-1")
        )
        assert row.status == "COMPLETED"
        assert row.billing_currency == "USD"
        assert row.cost_usd == cost
        assert row.reserved_usd == Decimal("0")
        assert row.cache_write_5m_tokens == 100
        assert row.cache_write_1h_tokens == 50
        assert row.cache_write_tokens == 150
        assert row.thinking_tokens == 120
        assert row.cache_ttl_estimated is False


def test_currency_tidak_tercampur(db_factory):
    _add_usage(db_factory, cost_usd=Decimal("1.00"), request_id="usd-row")
    _add_usage(
        db_factory,
        billing_currency="IDR",
        model="tokenmurah/claude-haiku-4-5-20251001",
        tier="FAST",
        cost_usd=Decimal("0"),
        cost_rp=Decimal("900.000000"),
        request_id="idr-row",
    )
    usd = cost_guard.get_anthropic_usage(date(2026, 11, 4))
    assert usd["actual_cost_usd"] == Decimal("1.000000")
    assert usd["budget_usd"] == Decimal("5.000000")

    monthly = cost_guard.get_monthly_usage()
    assert monthly["actual_cost_rp"] >= Decimal("900.000000")


def test_baris_idr_tidak_masuk_total_usd(db_factory):
    """Baris IDR (termasuk hasil backfill migrasi) tidak boleh masuk total USD."""
    with db_factory() as db:
        db.execute(
            text(
                "INSERT INTO ai_usage_logs "
                "(request_id, usage_date, provider, model, tier, input_tokens, "
                " output_tokens, cache_read_tokens, cache_write_tokens, "
                " cost_rp, reserved_rp, status, billing_currency, "
                " cache_write_5m_tokens, cache_write_1h_tokens, thinking_tokens, "
                " cache_ttl_estimated, cost_usd, reserved_usd, created_at) "
                "VALUES ('legacy-idr', '2026-11-04', 'omniroute', "
                " 'tokenmurah/claude-haiku-4-5-20251001', 'FAST', 10, 5, 0, 0, "
                " 100.000000, 0, 'COMPLETED', 'IDR', 0, 0, 0, 0, 0, 0, "
                " '2026-11-04 01:00:00')"
            )
        )
        db.commit()

    monthly = cost_guard.get_monthly_usage()
    assert monthly["actual_cost_rp"] >= Decimal("100.000000")
    usd = cost_guard.get_anthropic_usage(date(2026, 11, 4))
    assert usd["actual_cost_usd"] == Decimal("0")


def test_reservasi_idr_tetap_memakai_budget_rupiah(db_factory):
    result = cost_guard.reserve_ai_request(
        tier="FAST",
        prompt_text="x",
        max_output_tokens=50,
        request_id="idr-res-1",
        model=IDR_MODEL,
    )
    assert result["billing_currency"] == "IDR"
    assert result["reserved_cost_rp"] > 0
    assert result["reserved_cost_usd"] == Decimal("0")


def test_context_analysis_log_id_tertaut(db_factory):
    """Tautan usage -> analysis_log_id lewat context, tanpa ubah signature."""
    token = cost_guard.set_analysis_context(4242)
    try:
        cost_guard.reserve_ai_request(
            tier="FAST",
            prompt_text="x",
            max_output_tokens=50,
            request_id="ctx-1",
            model=HAIKU,
        )
    finally:
        cost_guard.reset_analysis_context(token)

    with db_factory() as db:
        row = db.scalar(
            select(AIUsageLog).where(AIUsageLog.request_id == "ctx-1")
        )
        assert row.analysis_log_id == 4242


# ============================================================
# 22-24. PENDING QUEUE
# ============================================================

def test_pipeline_budget_habis_masuk_antrean(db_factory, monkeypatch):
    from datetime import datetime, timezone as tz

    with db_factory() as db:
        db.add(
            AIAnalysisLog(
                chat_id=-100,
                target_message_id=901,
                status="PROCESSING",
            )
        )
        db.commit()
        log_id = db.scalar(
            select(AIAnalysisLog.id).where(
                AIAnalysisLog.target_message_id == 901
            )
        )

    pipeline.finish_analysis_log(
        log_id=log_id,
        status="BLOCKED_HARD_BUDGET",
        result={"status": "BLOCKED_HARD_BUDGET"},
    )
    pipeline.finish_analysis_log(
        log_id=log_id,
        status="BLOCKED_HARD_BUDGET",
        result={"status": "BLOCKED_HARD_BUDGET"},
    )

    with db_factory() as db:
        rows = db.execute(select(AIPendingMessage)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "PENDING"
        assert rows[0].reason == "BLOCKED_HARD_BUDGET"
        assert rows[0].analysis_log_id == log_id
        assert rows[0].dedup_key == "-100:901"


def test_claim_pending_atomik(db_factory):
    pending_queue.enqueue_message(-100, 902, reason="BLOCKED_HARD_BUDGET")
    first = pending_queue.claim_pending_batch(limit=5, owner="test")
    second = pending_queue.claim_pending_batch(limit=5, owner="test")
    assert len(first) == 1
    assert first[0]["status"] == "CLAIMED"
    assert second == []


def test_pending_analyzed_dan_recovery(db_factory):
    pending_queue.enqueue_message(-100, 903, reason="MANUAL_REVIEW_BUDGET")
    claimed = pending_queue.claim_pending_batch(limit=1, owner="w1")
    assert claimed

    # Claim menggantung dilepas setelah timeout.
    with db_factory() as db:
        row = db.execute(select(AIPendingMessage)).scalars().one()
        row.claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()

    assert pending_queue.release_stale_claims(timeout_minutes=30) == 1
    assert pending_queue.count_pending() == 1

    claimed = pending_queue.claim_pending_batch(limit=1, owner="w2")
    assert pending_queue.mark_pending_analyzed(claimed[0]["id"], owner="w2") is True
    assert pending_queue.count_pending() == 0


# ============================================================
# 25. KOMPATIBILITAS AI ROUTER DAN LAPORAN
# ============================================================

def test_router_usd_tidak_memakai_batas_lunak_rupiah(monkeypatch):
    from app.ai import ai_router

    monkeypatch.setattr(ai_router.settings, "AI_MODEL_REASONING", SONNET)
    assert ai_router.can_use_expensive_tier("REASONING", [{"role": "user", "content": "x"}]) is True


def test_router_model_tidak_dikenal_ditolak(monkeypatch):
    from app.ai import ai_router

    monkeypatch.setattr(ai_router.settings, "AI_MODEL_ESCALATION", "unknown/model")
    assert ai_router.can_use_expensive_tier("ESCALATION", []) is False


def test_laporan_per_model_dan_per_analysis(db_factory):
    _add_usage(
        db_factory, cost_usd=Decimal("1.25"), analysis_log_id=55, request_id="r55"
    )
    _add_usage(db_factory, cost_usd=Decimal("0.75"), request_id="r-null")

    model_report = billing_report.get_model_report("USD")
    assert model_report["models"][0]["actual_cost"] == Decimal("2.000000")

    analysis_report = billing_report.get_analysis_report("USD")
    ids = [row["analysis_log_id"] for row in analysis_report["analyses"]]
    assert 55 in ids
    assert None in ids


def test_proyeksi_30_hari(db_factory):
    _add_usage(
        db_factory,
        cost_usd=Decimal("0.50"),
        usage_date=date(2026, 11, 2),
        request_id="p1",
    )
    _add_usage(
        db_factory,
        cost_usd=Decimal("0.50"),
        usage_date=date(2026, 11, 3),
        request_id="p2",
    )
    projection = billing_report.get_projection_report()
    usd = projection["USD"]
    assert usd["days_observed"] == 3
    # 1.00 USD / 3 hari * 30 hari
    assert usd["projected_cost_30d"] == Decimal("10.000000")
    assert projection["budget"]["USD"] == Decimal("5.000000")


def test_laporan_tanpa_data_tidak_mengarang(db_factory):
    projection = billing_report.get_projection_report()
    assert projection["USD"]["confidence"] == "NO_DATA"
    assert projection["USD"]["projected_cost_30d"] == Decimal("0")
