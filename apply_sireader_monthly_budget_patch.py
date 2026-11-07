from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()
CONFIG = ROOT / "app" / "config.py"
ENV = ROOT / ".env"
COST_GUARD = ROOT / "app" / "ai" / "cost_guard.py"
TESTS = ROOT / "test_cost_guard_monthly.py"
RESERVATION_TESTS = ROOT / "test_cost_guard_reservation.py"
MARKER = "# SIREADER_MONTHLY_BUDGET_V1"

COST_GUARD_SOURCE = r'''from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import AIUsageLog


# SIREADER_MONTHLY_BUDGET_V1
# Satu lock PostgreSQL untuk reservasi global dan per model.
BUDGET_LOCK_KEY = 593104728391245

ONE_MILLION = Decimal("1000000")
MONEY_PRECISION = Decimal("0.000001")
SAFETY_FACTOR = Decimal("1.5")


class BudgetExceededError(Exception):
    """Dikeluarkan ketika kuota bulanan atau batas harian tidak cukup."""


class DuplicateRequestError(Exception):
    """Mencegah request ID yang sama diproses dua kali."""


def current_usage_date():
    """Tanggal penggunaan berdasarkan timezone SIREADER."""
    return datetime.now(ZoneInfo(settings.TIMEZONE)).date()


def current_period_start(usage_date=None):
    """Awal jendela pemakaian bergulir, termasuk hari ini (30 hari default)."""
    usage_date = usage_date or current_usage_date()
    period_days = int(settings.AI_BUDGET_PERIOD_DAYS)
    if period_days < 1:
        raise ValueError("AI_BUDGET_PERIOD_DAYS minimal 1.")
    return usage_date - timedelta(days=period_days - 1)


def _money(value):
    return Decimal(str(value)).quantize(
        MONEY_PRECISION,
        rounding=ROUND_CEILING,
    )


def _validate_tokens(**tokens):
    for name, value in tokens.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} harus berupa integer.")
        if value < 0:
            raise ValueError(f"{name} tidak boleh negatif.")


def get_tier_config(tier):
    """Mengambil model, harga per juta token, dan batas output per tier."""
    configs = {
        "FAST": {
            "model": settings.AI_MODEL_FAST,
            "input_price": settings.AI_FAST_INPUT_PRICE,
            "output_price": settings.AI_FAST_OUTPUT_PRICE,
            "cache_read_price": settings.AI_FAST_CACHE_READ_PRICE,
            "cache_write_price": settings.AI_FAST_CACHE_WRITE_PRICE,
            "max_output": settings.AI_MAX_OUTPUT_FAST,
        },
        "REASONING": {
            "model": settings.AI_MODEL_REASONING,
            "input_price": settings.AI_REASONING_INPUT_PRICE,
            "output_price": settings.AI_REASONING_OUTPUT_PRICE,
            "cache_read_price": settings.AI_REASONING_CACHE_READ_PRICE,
            "cache_write_price": settings.AI_REASONING_CACHE_WRITE_PRICE,
            "max_output": settings.AI_MAX_OUTPUT_REASONING,
        },
        "ESCALATION": {
            "model": settings.AI_MODEL_ESCALATION,
            "input_price": settings.AI_ESCALATION_INPUT_PRICE,
            "output_price": settings.AI_ESCALATION_OUTPUT_PRICE,
            "cache_read_price": settings.AI_ESCALATION_CACHE_READ_PRICE,
            "cache_write_price": settings.AI_ESCALATION_CACHE_WRITE_PRICE,
            "max_output": settings.AI_MAX_OUTPUT_ESCALATION,
        },
    }
    tier = tier.upper()
    if tier not in configs:
        raise ValueError(f"Tier AI tidak dikenal: {tier}")
    return configs[tier]


def get_monthly_budget_rp(tier):
    """Mengambil alokasi rupiah 30 hari untuk sebuah tier."""
    budgets = {
        "FAST": settings.AI_FAST_MONTHLY_BUDGET_RP,
        "REASONING": settings.AI_REASONING_MONTHLY_BUDGET_RP,
        "ESCALATION": settings.AI_ESCALATION_MONTHLY_BUDGET_RP,
    }
    tier = tier.upper()
    if tier not in budgets:
        raise ValueError(f"Tier AI tidak dikenal: {tier}")
    return Decimal(str(budgets[tier]))


def calculate_cost_rp(
    tier,
    input_tokens,
    output_tokens,
    cache_read_tokens=0,
    cache_write_tokens=0,
):
    """Biaya aktual; semua tarif adalah rupiah per satu juta token.

    input_tokens tidak memasukkan cache read/write. Token cache dicatat terpisah.
    """
    _validate_tokens(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    config = get_tier_config(tier)
    total = (
        Decimal(input_tokens) * Decimal(str(config["input_price"]))
        + Decimal(output_tokens) * Decimal(str(config["output_price"]))
        + Decimal(cache_read_tokens) * Decimal(str(config["cache_read_price"]))
        + Decimal(cache_write_tokens) * Decimal(str(config["cache_write_price"]))
    ) / ONE_MILLION
    return _money(total)


def estimate_input_tokens(prompt_text):
    """Estimasi konservatif berbasis byte UTF-8; bukan tokenizer resmi."""
    if not isinstance(prompt_text, str):
        raise TypeError("prompt_text harus berupa string.")
    return max(1, len(prompt_text.encode("utf-8")))


def estimate_request_cost(
    tier,
    prompt_text,
    max_output_tokens=None,
    extra_input_tokens=0,
):
    """Estimasi reservasi dengan asumsi input pada tarif termahal dan safety 50%."""
    _validate_tokens(extra_input_tokens=extra_input_tokens)
    config = get_tier_config(tier)
    input_tokens = estimate_input_tokens(prompt_text) + extra_input_tokens
    output_tokens = (
        max_output_tokens if max_output_tokens is not None else config["max_output"]
    )
    _validate_tokens(input_tokens=input_tokens, output_tokens=output_tokens)

    worst_input_price = max(
        config["input_price"],
        config["cache_read_price"],
        config["cache_write_price"],
    )
    estimated = _money(
        (
            Decimal(input_tokens) * Decimal(str(worst_input_price))
            + Decimal(output_tokens) * Decimal(str(config["output_price"]))
        ) / ONE_MILLION
    )
    reserved = _money(estimated * SAFETY_FACTOR)
    return {
        "estimated_input_tokens": input_tokens,
        "reserved_output_tokens": output_tokens,
        "estimated_cost_rp": estimated,
        "reserved_cost_rp": reserved,
    }


def _lock_budget(db):
    """Mengunci reservasi global agar request paralel tidak melewati kuota."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": BUDGET_LOCK_KEY},
    )


def _sum_costs(db, filters):
    actual = db.scalar(
        select(func.coalesce(func.sum(AIUsageLog.cost_rp), 0)).where(*filters)
    )
    reserved = db.scalar(
        select(func.coalesce(func.sum(AIUsageLog.reserved_rp), 0)).where(*filters)
    )
    return Decimal(str(actual or 0)), Decimal(str(reserved or 0))


def _daily_totals(db, usage_date):
    """Total penggunaan seluruh model pada tanggal lokal hari ini."""
    return _sum_costs(db, [AIUsageLog.usage_date == usage_date])


def _monthly_totals(db, usage_date, tier=None):
    """Total aktual + reservasi pada jendela 30 hari bergulir, global atau per tier."""
    filters = [
        AIUsageLog.usage_date >= current_period_start(usage_date),
        AIUsageLog.usage_date <= usage_date,
    ]
    if tier is not None:
        filters.append(AIUsageLog.tier == tier.upper())
    return _sum_costs(db, filters)


def _monthly_token_totals(db, usage_date, tier=None, reserved=False):
    """Token aktual dari COMPLETED atau token estimasi dari RESERVED/UNKNOWN."""
    filters = [
        AIUsageLog.usage_date >= current_period_start(usage_date),
        AIUsageLog.usage_date <= usage_date,
    ]
    if tier is not None:
        filters.append(AIUsageLog.tier == tier.upper())
    if reserved:
        filters.append(AIUsageLog.status.in_(("RESERVED", "UNKNOWN")))
    else:
        filters.append(AIUsageLog.status == "COMPLETED")

    names = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    values = {}
    for name in names:
        column = getattr(AIUsageLog, name)
        values[name] = int(
            db.scalar(
                select(func.coalesce(func.sum(column), 0)).where(*filters)
            ) or 0
        )
    values["total_tokens"] = sum(values.values())
    return values


def get_daily_usage():
    """Snapshot harian kompatibel dengan dashboard/diagnostik lama."""
    usage_date = current_usage_date()
    with SessionLocal() as db:
        actual, reserved = _daily_totals(db, usage_date)
    projected = actual + reserved
    target = Decimal(str(settings.AI_TARGET_DAILY_BUDGET_RP))
    hard_limit = Decimal(str(settings.AI_HARD_DAILY_LIMIT_RP))
    return {
        "date": usage_date.isoformat(),
        "actual_cost_rp": _money(actual),
        "reserved_cost_rp": _money(reserved),
        "projected_cost_rp": _money(projected),
        "target_rp": target,
        "hard_limit_rp": hard_limit,
        "target_reached": projected >= target,
        "hard_limit_reached": projected >= hard_limit,
    }


def get_monthly_usage():
    """Snapshot global dan per tier untuk jendela 30 hari bergulir."""
    usage_date = current_usage_date()
    period_start = current_period_start(usage_date)
    with SessionLocal() as db:
        global_actual, global_reserved = _monthly_totals(db, usage_date)
        global_actual_tokens = _monthly_token_totals(db, usage_date)
        global_reserved_tokens = _monthly_token_totals(db, usage_date, reserved=True)
        tiers = {}
        for tier in ("FAST", "REASONING", "ESCALATION"):
            actual, reserved = _monthly_totals(db, usage_date, tier=tier)
            projected = actual + reserved
            budget = get_monthly_budget_rp(tier)
            tiers[tier] = {
                "budget_rp": budget,
                "actual_cost_rp": _money(actual),
                "reserved_cost_rp": _money(reserved),
                "projected_cost_rp": _money(projected),
                "remaining_rp": _money(max(Decimal("0"), budget - projected)),
                "budget_reached": projected >= budget,
                "actual_tokens": _monthly_token_totals(db, usage_date, tier=tier),
                "reserved_tokens": _monthly_token_totals(db, usage_date, tier=tier, reserved=True),
            }
    projected_global = global_actual + global_reserved
    global_budget = Decimal(str(settings.AI_MONTHLY_BUDGET_RP))
    return {
        "period_start": period_start.isoformat(),
        "period_end": usage_date.isoformat(),
        "period_days": int(settings.AI_BUDGET_PERIOD_DAYS),
        "budget_rp": global_budget,
        "actual_cost_rp": _money(global_actual),
        "reserved_cost_rp": _money(global_reserved),
        "projected_cost_rp": _money(projected_global),
        "remaining_rp": _money(max(Decimal("0"), global_budget - projected_global)),
        "budget_reached": projected_global >= global_budget,
        "actual_tokens": global_actual_tokens,
        "reserved_tokens": global_reserved_tokens,
        "tiers": tiers,
    }


def reserve_ai_request(
    tier,
    prompt_text,
    max_output_tokens=None,
    extra_input_tokens=0,
    request_id=None,
):
    """Reservasi atomik dengan batas harian keras, bulanan global, dan bulanan per tier."""
    tier = tier.upper()
    config = get_tier_config(tier)
    estimate = estimate_request_cost(
        tier=tier,
        prompt_text=prompt_text,
        max_output_tokens=max_output_tokens,
        extra_input_tokens=extra_input_tokens,
    )
    request_id = request_id or uuid4().hex
    usage_date = current_usage_date()
    new_reserved = estimate["reserved_cost_rp"]

    with SessionLocal() as db:
        _lock_budget(db)
        existing = db.scalar(
            select(AIUsageLog).where(AIUsageLog.request_id == request_id)
        )
        if existing:
            raise DuplicateRequestError("Request ID sudah pernah digunakan.")

        # Batas keras harian lama dipertahankan sebagai lapisan keselamatan tambahan.
        daily_actual, daily_reserved = _daily_totals(db, usage_date)
        daily_projected = daily_actual + daily_reserved
        daily_hard_limit = Decimal(str(settings.AI_HARD_DAILY_LIMIT_RP))
        if daily_projected + new_reserved > daily_hard_limit:
            raise BudgetExceededError(
                "AI request ditolak: reservasi melewati batas keras harian."
            )

        monthly_actual, monthly_reserved = _monthly_totals(db, usage_date)
        monthly_projected = monthly_actual + monthly_reserved
        monthly_budget = Decimal(str(settings.AI_MONTHLY_BUDGET_RP))
        if monthly_projected + new_reserved > monthly_budget:
            raise BudgetExceededError(
                "AI request ditolak: kuota global 30 hari tidak mencukupi."
            )

        tier_actual, tier_reserved = _monthly_totals(
            db, usage_date, tier=tier
        )
        tier_projected = tier_actual + tier_reserved
        tier_budget = get_monthly_budget_rp(tier)
        if tier_projected + new_reserved > tier_budget:
            raise BudgetExceededError(
                f"AI request ditolak: kuota 30 hari tier {tier} tidak mencukupi."
            )

        log = AIUsageLog(
            request_id=request_id,
            usage_date=usage_date,
            provider=settings.AI_PROVIDER,
            model=config["model"],
            tier=tier,
            # Kolom token menyimpan estimasi selama RESERVED/UNKNOWN.
            # finalize_ai_request akan menggantinya dengan usage aktual.
            input_tokens=estimate["estimated_input_tokens"],
            output_tokens=estimate["reserved_output_tokens"],
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_rp=Decimal("0"),
            reserved_rp=new_reserved,
            status="RESERVED",
        )
        db.add(log)
        db.commit()

    return {
        "request_id": request_id,
        "tier": tier,
        "model": config["model"],
        "estimated_input_tokens": estimate["estimated_input_tokens"],
        "reserved_output_tokens": estimate["reserved_output_tokens"],
        "estimated_cost_rp": estimate["estimated_cost_rp"],
        "reserved_cost_rp": new_reserved,
        "projected_monthly_cost_rp": _money(monthly_projected + new_reserved),
        "tier_budget_rp": tier_budget,
        "projected_tier_cost_rp": _money(tier_projected + new_reserved),
        "period_start": current_period_start(usage_date).isoformat(),
        "period_days": int(settings.AI_BUDGET_PERIOD_DAYS),
    }


def finalize_ai_request(
    request_id,
    input_tokens,
    output_tokens,
    cache_read_tokens=0,
    cache_write_tokens=0,
):
    """Mengganti reservasi dengan biaya aktual setelah usage provider diterima."""
    _validate_tokens(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    with SessionLocal() as db:
        _lock_budget(db)
        log = db.scalar(
            select(AIUsageLog)
            .where(AIUsageLog.request_id == request_id)
            .with_for_update()
        )
        if not log:
            raise ValueError("Request ID tidak ditemukan.")
        if log.status == "COMPLETED":
            return _money(log.cost_rp)
        if log.status not in ("RESERVED", "UNKNOWN"):
            raise ValueError(f"Request tidak bisa diselesaikan: {log.status}")

        cost = calculate_cost_rp(
            tier=log.tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        log.input_tokens = input_tokens
        log.output_tokens = output_tokens
        log.cache_read_tokens = cache_read_tokens
        log.cache_write_tokens = cache_write_tokens
        log.cost_rp = cost
        log.reserved_rp = Decimal("0")
        log.status = "COMPLETED"
        log.completed_at = datetime.now().astimezone()
        db.commit()
    return cost


def mark_ai_request_failed(
    request_id,
    error_message,
    definitely_not_charged=False,
):
    """Lepas reservasi hanya jika terbukti request tidak ditagih."""
    with SessionLocal() as db:
        _lock_budget(db)
        log = db.scalar(
            select(AIUsageLog)
            .where(AIUsageLog.request_id == request_id)
            .with_for_update()
        )
        if not log:
            raise ValueError("Request ID tidak ditemukan.")
        if log.status != "RESERVED":
            return log.status
        log.error_message = str(error_message)[:1000]
        if definitely_not_charged:
            log.status = "FAILED"
            log.reserved_rp = Decimal("0")
            log.input_tokens = 0
            log.output_tokens = 0
            log.cache_read_tokens = 0
            log.cache_write_tokens = 0
            final_status = "FAILED"
        else:
            log.status = "UNKNOWN"
            final_status = "UNKNOWN"
        db.commit()
    return final_status
'''

MONTHLY_TESTS = r'''from datetime import date, timedelta
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
'''

PRICES = {
    "AI_FAST_INPUT_PRICE": 528,
    "AI_FAST_OUTPUT_PRICE": 2640,
    "AI_FAST_CACHE_READ_PRICE": 53,
    "AI_FAST_CACHE_WRITE_PRICE": 660,
    "AI_REASONING_INPUT_PRICE": 1346,
    "AI_REASONING_OUTPUT_PRICE": 6732,
    "AI_REASONING_CACHE_READ_PRICE": 135,
    "AI_REASONING_CACHE_WRITE_PRICE": 1683,
    "AI_ESCALATION_INPUT_PRICE": 4749,
    "AI_ESCALATION_OUTPUT_PRICE": 23747,
    "AI_ESCALATION_CACHE_READ_PRICE": 475,
    "AI_ESCALATION_CACHE_WRITE_PRICE": 5937,
}

BUDGETS = {
    "AI_MONTHLY_BUDGET_RP": 50000,
    "AI_BUDGET_PERIOD_DAYS": 30,
    "AI_FAST_MONTHLY_BUDGET_RP": 25000,
    "AI_REASONING_MONTHLY_BUDGET_RP": 22500,
    "AI_ESCALATION_MONTHLY_BUDGET_RP": 2500,
}


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_suffix(path.suffix + f".{stamp}.bak")
    shutil.copy2(path, target)
    return target


def replace_or_add_env(text: str, values: dict[str, int]) -> str:
    lines = text.splitlines()
    seen = set()
    out = []
    for line in lines:
        match = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if match and match.group(1) in values:
            name = match.group(1)
            if name not in seen:
                out.append(f"{name}={values[name]}")
                seen.add(name)
            # Drop duplicate definitions for the same managed key.
        else:
            out.append(line)
    missing = [f"{name}={value}" for name, value in values.items() if name not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.extend(["# SIREADER monthly AI budget and TokenMurah prices"] + missing)
    return "\n".join(out) + "\n"


def update_config_fields(text: str, values: dict[str, int]) -> str:
    missing = {}
    for name, value in values.items():
        pattern = re.compile(
            rf"(?m)^([ \t]+{re.escape(name)}\s*(?::[^=\n]+)?=\s*)[^\n]*$"
        )
        if pattern.search(text):
            text = pattern.sub(lambda m: m.group(1) + str(value), text, count=1)
        else:
            missing[name] = value

    if missing:
        anchors = [
            re.search(r"(?m)^[ \t]+model_config\s*=", text),
            re.search(r"(?m)^settings\s*=\s*Settings\s*\(", text),
        ]
        anchor = next((m for m in anchors if m), None)
        if not anchor:
            raise RuntimeError(
                "Tidak menemukan anchor untuk menambahkan field ke class Settings. "
                "Tidak ada perubahan config yang disimpan."
            )
        insert_at = anchor.start()
        block = "".join(f"    {name}: int = {value}\n" for name, value in missing.items())
        text = text[:insert_at] + block + "\n" + text[insert_at:]
    return text


def main():
    for path in (CONFIG, ENV, COST_GUARD, RESERVATION_TESTS):
        if not path.exists():
            raise SystemExit(f"File wajib tidak ditemukan: {path}")

    config_text = CONFIG.read_text(encoding="utf-8")
    env_text = ENV.read_text(encoding="utf-8")
    cost_text = COST_GUARD.read_text(encoding="utf-8")
    reservation_test_text = RESERVATION_TESTS.read_text(encoding="utf-8")

    if MARKER in cost_text:
        raise SystemExit("Patch bulanan sudah pernah diterapkan. Tidak ada perubahan.")

    # Validasi class Settings ada sebelum membuat backup.
    if not re.search(r"(?m)^class\s+Settings\b", config_text):
        raise SystemExit("Class Settings tidak ditemukan di app/config.py")

    backups = [backup(p) for p in (CONFIG, ENV, COST_GUARD, RESERVATION_TESTS)]
    try:
        config_values = {
            **PRICES,
            **BUDGETS,
            # Batas harian kompatibilitas disamakan dengan total monthly cap.
            "AI_TARGET_DAILY_BUDGET_RP": 50000,
            "AI_HARD_DAILY_LIMIT_RP": 50000,
        }
        new_config = update_config_fields(config_text, config_values)
        new_env = replace_or_add_env(
            env_text,
            {
                **PRICES,
                **BUDGETS,
                "AI_TARGET_DAILY_BUDGET_RP": 50000,
                "AI_HARD_DAILY_LIMIT_RP": 50000,
            },
        )
        CONFIG.write_text(new_config, encoding="utf-8")
        ENV.write_text(new_env, encoding="utf-8")
        COST_GUARD.write_text(COST_GUARD_SOURCE, encoding="utf-8")
        # Existing reservation test asserted the previous FAST tariff.
        updated_reservation_tests = reservation_test_text.replace(
            'Decimal("0.792600")', 'Decimal("0.835600")'
        )
        RESERVATION_TESTS.write_text(updated_reservation_tests, encoding="utf-8")
        TESTS.write_text(MONTHLY_TESTS, encoding="utf-8")
    except Exception:
        # Restore files if patching fails partway through.
        for original, saved in zip((CONFIG, ENV, COST_GUARD, RESERVATION_TESTS), backups):
            shutil.copy2(saved, original)
        raise

    print("PATCH BERHASIL DITERAPKAN")
    print("Backup dibuat:")
    for p in backups:
        print(f" - {p.name}")
    print("File diperbarui: app/config.py, .env, app/ai/cost_guard.py, test_cost_guard_reservation.py")
    print("Tes baru dibuat: test_cost_guard_monthly.py")
    print("Anggaran bulanan: Rp50.000 / 30 hari")
    print("Alokasi FAST/REASONING/ESCALATION: Rp25.000 / Rp22.500 / Rp2.500")
    print("Tarif diset sesuai tabel TokenMurah yang diberikan pengguna.")
    print("AI_PIPELINE_ENABLED tidak diubah.")


if __name__ == "__main__":
    main()
