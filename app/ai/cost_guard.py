from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from contextvars import ContextVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import AIUsageAlert, AIUsageLog


# SIREADER_MONTHLY_BUDGET_V1 (IDR / TokenMurah) + ANTHROPIC_USD_V1
# Satu lock PostgreSQL untuk reservasi global dan per tier.
BUDGET_LOCK_KEY = 593104728391245

ONE_MILLION = Decimal("1000000")
MONEY_PRECISION = Decimal("0.000001")
SAFETY_FACTOR = Decimal("1.5")

CURRENCY_IDR = "IDR"
CURRENCY_USD = "USD"

IDR_MODEL_PREFIX = "tokenmurah/"
FAMILY_HAIKU = "HAIKU"
FAMILY_SONNET = "SONNET"
FAMILY_OPUS = "OPUS"


class BudgetExceededError(Exception):
    """Dikeluarkan ketika kuota bulanan atau batas harian tidak cukup."""


class DuplicateRequestError(Exception):
    """Mencegah request ID yang sama diproses dua kali."""


class UnknownModelPricingError(ValueError):
    """Model/provider tidak dikenal; reservasi digagalkan secara aman."""


# Tautan opsional usage -> ai_analysis_logs.id tanpa mengubah signature
# pemanggil lama. Pipeline men-set sebelum analisis, lalu me-reset.
_ANALYSIS_LOG_ID: ContextVar = ContextVar("sireader_analysis_log_id", default=None)


def set_analysis_context(analysis_log_id):
    return _ANALYSIS_LOG_ID.set(analysis_log_id)


def reset_analysis_context(token):
    _ANALYSIS_LOG_ID.reset(token)


def current_analysis_log_id():
    return _ANALYSIS_LOG_ID.get()


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
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} harus berupa integer.")
        if value < 0:
            raise ValueError(f"{name} tidak boleh negatif.")


# ============================================================
# IDENTITAS PROVIDER / MATA UANG
# ============================================================

def model_prefix(model):
    """Prefix provider sebuah model id, mis. 'anthropic/'."""
    if not isinstance(model, str) or "/" not in model:
        return ""
    return model.split("/", 1)[0].strip().lower() + "/"


def model_family(model):
    """Keluarga harga berdasarkan nama model, bukan berdasarkan tier."""
    name = str(model).lower()
    if "haiku" in name:
        return FAMILY_HAIKU
    if "sonnet" in name:
        return FAMILY_SONNET
    if "opus" in name:
        return FAMILY_OPUS
    return None


def model_currency(model):
    """IDR hanya untuk TokenMurah, USD hanya untuk prefix Anthropic."""
    prefix = model_prefix(model)
    if prefix == IDR_MODEL_PREFIX:
        return CURRENCY_IDR
    if prefix == str(settings.AI_ANTHROPIC_MODEL_PREFIX).strip().lower():
        return CURRENCY_USD
    return None


def resolve_billing(model):
    """Menyelesaikan currency dan keluarga harga. Gagal aman bila tidak dikenal."""
    currency = model_currency(model)
    family = model_family(model)
    if currency is None:
        raise UnknownModelPricingError(
            f"Provider model tidak dikenal: {model!r}. "
            "Reservasi dibatalkan agar mata uang tidak salah pilih."
        )
    if family is None:
        raise UnknownModelPricingError(
            f"Keluarga model tidak dikenal: {model!r}. "
            "Reservasi dibatalkan agar tarif tidak salah pilih."
        )
    return {
        "model": model,
        "currency": currency,
        "family": family,
        "long_context_capable": (
            family == FAMILY_HAIKU and currency == CURRENCY_USD
        ),
    }


def _usd_price_table(family):
    """Tarif USD per 1 juta token; tarif long hanya ada untuk Haiku."""
    table = {
        FAMILY_HAIKU: {
            "input": settings.AI_ANTHROPIC_HAIKU_INPUT_USD,
            "output": settings.AI_ANTHROPIC_HAIKU_OUTPUT_USD,
            "cache_write_5m": settings.AI_ANTHROPIC_HAIKU_CACHE_WRITE_5M_USD,
            "cache_write_1h": settings.AI_ANTHROPIC_HAIKU_CACHE_WRITE_1H_USD,
            "cache_read": settings.AI_ANTHROPIC_HAIKU_CACHE_READ_USD,
            "long": {
                "input": settings.AI_ANTHROPIC_HAIKU_LONG_INPUT_USD,
                "output": settings.AI_ANTHROPIC_HAIKU_LONG_OUTPUT_USD,
                "cache_write_5m": settings.AI_ANTHROPIC_HAIKU_LONG_CACHE_WRITE_5M_USD,
                "cache_write_1h": settings.AI_ANTHROPIC_HAIKU_LONG_CACHE_WRITE_1H_USD,
                "cache_read": settings.AI_ANTHROPIC_HAIKU_LONG_CACHE_READ_USD,
            },
        },
        FAMILY_SONNET: {
            "input": settings.AI_ANTHROPIC_SONNET_INPUT_USD,
            "output": settings.AI_ANTHROPIC_SONNET_OUTPUT_USD,
            "cache_write_5m": settings.AI_ANTHROPIC_SONNET_CACHE_WRITE_5M_USD,
            "cache_write_1h": settings.AI_ANTHROPIC_SONNET_CACHE_WRITE_1H_USD,
            "cache_read": settings.AI_ANTHROPIC_SONNET_CACHE_READ_USD,
            "long": None,
        },
        FAMILY_OPUS: {
            "input": settings.AI_ANTHROPIC_OPUS_INPUT_USD,
            "output": settings.AI_ANTHROPIC_OPUS_OUTPUT_USD,
            "cache_write_5m": settings.AI_ANTHROPIC_OPUS_CACHE_WRITE_5M_USD,
            "cache_write_1h": settings.AI_ANTHROPIC_OPUS_CACHE_WRITE_1H_USD,
            "cache_read": settings.AI_ANTHROPIC_OPUS_CACHE_READ_USD,
            "long": None,
        },
    }
    return table[family]


def budget_for_currency(currency):
    currency = str(currency).upper()
    if currency == CURRENCY_USD:
        return _money(settings.AI_ANTHROPIC_MONTHLY_BUDGET_USD)
    return Decimal(str(settings.AI_MONTHLY_BUDGET_RP))


def warning_thresholds():
    """Ambang alert dari konfigurasi, mis. 70, 85, 95 (persen)."""
    raw = str(settings.AI_ANTHROPIC_WARNING_THRESHOLDS)
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        number = Decimal(part)
        if number <= 0 or number > 1:
            raise ValueError(f"Ambang alert harus di antara 0 dan 1: {part!r}")
        values.append(int((number * 100).quantize(Decimal("1"))))
    return sorted(set(values))


# ============================================================
# TARIF IDR (TOKENMURAH) — TIDAK BERUBAH
# ============================================================

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


# ============================================================
# TARIF USD (ANTHROPIC)
# ============================================================

def estimate_request_usd(model, prompt_text, max_output_tokens=None):
    """Estimasi reservasi USD dengan tarif input termahal dan safety 50%."""
    billing = resolve_billing(model)
    if billing["currency"] != CURRENCY_USD:
        raise ValueError(f"Model {model!r} bukan provider USD.")

    input_tokens = estimate_input_tokens(prompt_text)
    if max_output_tokens is None:
        raise ValueError("max_output_tokens wajib untuk estimasi USD.")
    _validate_tokens(input_tokens=input_tokens, output_tokens=max_output_tokens)

    prices = _usd_price_table(billing["family"])
    threshold = int(settings.AI_ANTHROPIC_LONG_CONTEXT_TOKENS)
    if (
        billing["long_context_capable"]
        and input_tokens > threshold
        and prices["long"] is not None
    ):
        prices = prices["long"]

    worst_input = max(
        Decimal(str(prices["input"])),
        Decimal(str(prices["cache_read"])),
        Decimal(str(prices["cache_write_5m"])),
        Decimal(str(prices["cache_write_1h"])),
    )
    estimated = _money(
        (
            Decimal(input_tokens) * worst_input
            + Decimal(max_output_tokens) * Decimal(str(prices["output"]))
        )
        / ONE_MILLION
    )
    reserved = _money(estimated * SAFETY_FACTOR)
    return {
        "estimated_input_tokens": input_tokens,
        "reserved_output_tokens": max_output_tokens,
        "estimated_cost_usd": estimated,
        "reserved_cost_usd": reserved,
    }


def usd_cost_breakdown(
    model,
    input_tokens,
    output_tokens,
    cache_read_tokens=0,
    cache_write_5m_tokens=0,
    cache_write_1h_tokens=0,
    cache_write_tokens=0,
):
    """Rincian biaya USD. Thinking token TIDAK ditambahkan terpisah.

    cache_write_tokens tanpa rincian TTL dihitung dengan tarif write 1 jam
    (estimasi konservatif) dan ditandai cache_ttl_estimated=True.
    """
    _validate_tokens(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    billing = resolve_billing(model)
    if billing["currency"] != CURRENCY_USD:
        raise ValueError(f"Model {model!r} bukan provider USD.")

    ttl_estimated = False
    write_5m = int(cache_write_5m_tokens or 0)
    write_1h = int(cache_write_1h_tokens or 0)
    if not write_5m and not write_1h and cache_write_tokens:
        write_1h = int(cache_write_tokens)
        ttl_estimated = True

    prompt_total = int(input_tokens) + int(cache_read_tokens) + write_5m + write_1h
    threshold = int(settings.AI_ANTHROPIC_LONG_CONTEXT_TOKENS)
    long_context = False
    prices = _usd_price_table(billing["family"])
    if (
        billing["long_context_capable"]
        and prompt_total > threshold
        and prices["long"] is not None
    ):
        prices = prices["long"]
        long_context = True

    cost = (
        Decimal(input_tokens) * Decimal(str(prices["input"]))
        + Decimal(output_tokens) * Decimal(str(prices["output"]))
        + Decimal(cache_read_tokens) * Decimal(str(prices["cache_read"]))
        + Decimal(write_5m) * Decimal(str(prices["cache_write_5m"]))
        + Decimal(write_1h) * Decimal(str(prices["cache_write_1h"]))
    ) / ONE_MILLION

    return {
        "cost_usd": _money(cost),
        "billing_currency": CURRENCY_USD,
        "family": billing["family"],
        "long_context": long_context,
        "prompt_total_tokens": prompt_total,
        "cache_write_5m_tokens": write_5m,
        "cache_write_1h_tokens": write_1h,
        "cache_ttl_estimated": ttl_estimated,
    }


def calculate_cost_usd(
    model,
    input_tokens,
    output_tokens,
    cache_read_tokens=0,
    cache_write_5m_tokens=0,
    cache_write_1h_tokens=0,
    cache_write_tokens=0,
):
    return usd_cost_breakdown(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        cache_write_tokens=cache_write_tokens,
    )["cost_usd"]


# ============================================================
# TOTALS
# ============================================================

def _lock_budget(db):
    """Mengunci reservasi global agar request paralel tidak melewati kuota."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": BUDGET_LOCK_KEY},
    )


def _idr_currency_filter():
    # Baris lama (sebelum migrasi kolom) dianggap IDR.
    return or_(
        AIUsageLog.billing_currency == CURRENCY_IDR,
        AIUsageLog.billing_currency.is_(None),
    )


def _usd_currency_filter():
    return AIUsageLog.billing_currency == CURRENCY_USD


def _sum_costs(db, filters):
    """Total IDR; baris lama tanpa billing_currency ikut dihitung."""
    filters = list(filters) + [_idr_currency_filter()]
    actual = db.scalar(
        select(func.coalesce(func.sum(AIUsageLog.cost_rp), 0)).where(*filters)
    )
    reserved = db.scalar(
        select(func.coalesce(func.sum(AIUsageLog.reserved_rp), 0)).where(*filters)
    )
    return Decimal(str(actual or 0)), Decimal(str(reserved or 0))


def _sum_costs_usd(db, filters):
    """Total USD; baris IDR tidak pernah ikut dijumlah."""
    filters = list(filters) + [_usd_currency_filter()]
    actual = db.scalar(
        select(func.coalesce(func.sum(AIUsageLog.cost_usd), 0)).where(*filters)
    )
    reserved = db.scalar(
        select(func.coalesce(func.sum(AIUsageLog.reserved_usd), 0)).where(*filters)
    )
    return Decimal(str(actual or 0)), Decimal(str(reserved or 0))


def _daily_totals(db, usage_date):
    """Total penggunaan seluruh model pada tanggal lokal hari ini."""
    return _sum_costs(db, [AIUsageLog.usage_date == usage_date])


def _monthly_totals(db, usage_date, tier=None):
    """Total aktual + reservasi IDR pada jendela 30 hari bergulir."""
    filters = [
        AIUsageLog.usage_date >= current_period_start(usage_date),
        AIUsageLog.usage_date <= usage_date,
    ]
    if tier is not None:
        filters.append(AIUsageLog.tier == tier.upper())
    return _sum_costs(db, filters)


def _monthly_usd_totals(db, usage_date, tier=None):
    """Total aktual + reservasi USD pada jendela 30 hari bergulir."""
    filters = [
        AIUsageLog.usage_date >= current_period_start(usage_date),
        AIUsageLog.usage_date <= usage_date,
    ]
    if tier is not None:
        filters.append(AIUsageLog.tier == tier.upper())
    return _sum_costs_usd(db, filters)


def _monthly_token_totals(
    db,
    usage_date,
    tier=None,
    reserved=False,
    currency=CURRENCY_IDR,
):
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
    filters.append(
        _usd_currency_filter()
        if str(currency).upper() == CURRENCY_USD
        else _idr_currency_filter()
    )

    names = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "thinking_tokens",
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

def get_analysis_usage_status(analysis_log_id):
    """Ringkasan status transaksi AI untuk satu analisis."""
    if analysis_log_id is None:
        raise ValueError("analysis_log_id wajib diisi.")

    with SessionLocal() as db:
        rows = (
            db.execute(
                select(
                    AIUsageLog.request_id,
                    AIUsageLog.tier,
                    AIUsageLog.status,
                )
                .where(AIUsageLog.analysis_log_id == int(analysis_log_id))
                .order_by(AIUsageLog.created_at, AIUsageLog.id)
            )
            .all()
        )

    transactions = [
        {
            "request_id": row.request_id,
            "tier": row.tier,
            "status": row.status,
        }
        for row in rows
    ]

    statuses = [item["status"] for item in transactions]

    return {
        "analysis_log_id": int(analysis_log_id),
        "total": len(transactions),
        "completed": statuses.count("COMPLETED"),
        "failed": statuses.count("FAILED"),
        "reserved": statuses.count("RESERVED"),
        "unknown": statuses.count("UNKNOWN"),
        "transactions": transactions,
        "has_uncertain": any(
            status in ("RESERVED", "UNKNOWN")
            for status in statuses
        ),
        "all_terminal": all(
            status in ("COMPLETED", "FAILED")
            for status in statuses
        ),
    }



def get_daily_usage():
    """Snapshot harian IDR kompatibel dengan dashboard/diagnostik lama."""
    usage_date = current_usage_date()
    with SessionLocal() as db:
        actual, reserved = _daily_totals(db, usage_date)
    projected = actual + reserved
    target = Decimal(str(settings.AI_TARGET_DAILY_BUDGET_RP))
    hard_limit = Decimal(str(settings.AI_HARD_DAILY_LIMIT_RP))
    return {
        "currency": CURRENCY_IDR,
        "date": usage_date.isoformat(),
        "actual_cost_rp": _money(actual),
        "reserved_cost_rp": _money(reserved),
        "projected_cost_rp": _money(projected),
        "target_rp": target,
        "hard_limit_rp": hard_limit,
        "target_reached": projected >= target,
        "hard_limit_reached": projected >= hard_limit,
    }


def get_anthropic_usage(usage_date=None):
    """Snapshot budget Anthropic USD, terpisah penuh dari laporan Rupiah."""
    usage_date = usage_date or current_usage_date()
    budget = _money(settings.AI_ANTHROPIC_MONTHLY_BUDGET_USD)
    with SessionLocal() as db:
        actual, reserved = _monthly_usd_totals(db, usage_date)
        actual_tokens = _monthly_token_totals(db, usage_date, currency=CURRENCY_USD)
        reserved_tokens = _monthly_token_totals(
            db, usage_date, currency=CURRENCY_USD, reserved=True
        )
        tiers = {}
        for tier in ("FAST", "REASONING", "ESCALATION"):
            tier_actual, tier_reserved = _monthly_usd_totals(db, usage_date, tier=tier)
            # Alokasi tier hanya untuk monitoring; tidak ada hard limit terpisah.
            tiers[tier] = {
                "actual_cost_usd": _money(tier_actual),
                "reserved_cost_usd": _money(tier_reserved),
                "projected_cost_usd": _money(tier_actual + tier_reserved),
                "actual_tokens": _monthly_token_totals(
                    db, usage_date, tier=tier, currency=CURRENCY_USD
                ),
            }
    projected = actual + reserved
    percent = (projected / budget * Decimal("100")) if budget > 0 else Decimal("0")
    return {
        "currency": CURRENCY_USD,
        "period_start": current_period_start(usage_date).isoformat(),
        "period_end": usage_date.isoformat(),
        "period_days": int(settings.AI_BUDGET_PERIOD_DAYS),
        "budget_usd": budget,
        "actual_cost_usd": _money(actual),
        "reserved_cost_usd": _money(reserved),
        "projected_cost_usd": _money(projected),
        "remaining_usd": _money(max(Decimal("0"), budget - projected)),
        "percent_used": percent.quantize(Decimal("0.0001")),
        "budget_reached": projected >= budget,
        "actual_tokens": actual_tokens,
        "reserved_tokens": reserved_tokens,
        "tiers": tiers,
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
        "currency": CURRENCY_IDR,
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


# ============================================================
# RESERVASI
# ============================================================

def reserve_ai_request(
    tier,
    prompt_text,
    max_output_tokens=None,
    extra_input_tokens=0,
    request_id=None,
    model=None,
    analysis_log_id=None,
):
    """Reservasi atomik; currency ditentukan dari model/provider, bukan tier."""
    tier = tier.upper()
    config = get_tier_config(tier)
    model = model or config["model"]
    billing = resolve_billing(model)
    request_id = request_id or uuid4().hex
    if analysis_log_id is None:
        analysis_log_id = current_analysis_log_id()

    if billing["currency"] == CURRENCY_USD:
        return _reserve_usd(
            tier=tier,
            model=model,
            prompt_text=prompt_text,
            max_output_tokens=max_output_tokens,
            extra_input_tokens=extra_input_tokens,
            request_id=request_id,
            analysis_log_id=analysis_log_id,
            config=config,
        )

    return _reserve_idr(
        tier=tier,
        model=model,
        prompt_text=prompt_text,
        max_output_tokens=max_output_tokens,
        extra_input_tokens=extra_input_tokens,
        request_id=request_id,
        analysis_log_id=analysis_log_id,
        config=config,
    )


def _reserve_idr(
    tier,
    model,
    prompt_text,
    max_output_tokens,
    extra_input_tokens,
    request_id,
    analysis_log_id,
    config,
):
    estimate = estimate_request_cost(
        tier=tier,
        prompt_text=prompt_text,
        max_output_tokens=max_output_tokens,
        extra_input_tokens=extra_input_tokens,
    )
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

        tier_actual, tier_reserved = _monthly_totals(db, usage_date, tier=tier)
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
            model=model,
            tier=tier,
            # Kolom token menyimpan estimasi selama RESERVED/UNKNOWN.
            # finalize_ai_request akan menggantinya dengan usage aktual.
            input_tokens=estimate["estimated_input_tokens"],
            output_tokens=estimate["reserved_output_tokens"],
            cache_read_tokens=0,
            cache_write_tokens=0,
            billing_currency=CURRENCY_IDR,
            cost_rp=Decimal("0"),
            reserved_rp=new_reserved,
            cost_usd=Decimal("0"),
            reserved_usd=Decimal("0"),
            analysis_log_id=analysis_log_id,
            status="RESERVED",
        )
        db.add(log)
        db.commit()

    return {
        "request_id": request_id,
        "tier": tier,
        "model": model,
        "billing_currency": CURRENCY_IDR,
        "estimated_input_tokens": estimate["estimated_input_tokens"],
        "reserved_output_tokens": estimate["reserved_output_tokens"],
        "estimated_cost_rp": estimate["estimated_cost_rp"],
        "reserved_cost_rp": new_reserved,
        "estimated_cost_usd": Decimal("0"),
        "reserved_cost_usd": Decimal("0"),
        "projected_monthly_cost_rp": _money(monthly_projected + new_reserved),
        "tier_budget_rp": tier_budget,
        "projected_tier_cost_rp": _money(tier_projected + new_reserved),
        "period_start": current_period_start(usage_date).isoformat(),
        "period_days": int(settings.AI_BUDGET_PERIOD_DAYS),
    }


def _reserve_usd(
    tier,
    model,
    prompt_text,
    max_output_tokens,
    extra_input_tokens,
    request_id,
    analysis_log_id,
    config,
):
    output_limit = (
        max_output_tokens if max_output_tokens is not None else config["max_output"]
    )
    estimate = estimate_request_usd(
        model=model,
        prompt_text=prompt_text,
        max_output_tokens=output_limit,
    )
    _validate_tokens(extra_input_tokens=extra_input_tokens)
    usage_date = current_usage_date()
    new_reserved = estimate["reserved_cost_usd"]
    budget = _money(settings.AI_ANTHROPIC_MONTHLY_BUDGET_USD)

    with SessionLocal() as db:
        _lock_budget(db)
        existing = db.scalar(
            select(AIUsageLog).where(AIUsageLog.request_id == request_id)
        )
        if existing:
            raise DuplicateRequestError("Request ID sudah pernah digunakan.")

        monthly_actual, monthly_reserved = _monthly_usd_totals(db, usage_date)
        monthly_projected = monthly_actual + monthly_reserved
        if monthly_projected + new_reserved > budget:
            raise BudgetExceededError(
                "AI request ditolak: kuota Anthropic USD 30 hari tidak mencukupi."
            )

        log = AIUsageLog(
            request_id=request_id,
            usage_date=usage_date,
            provider=settings.AI_PROVIDER,
            model=model,
            tier=tier,
            input_tokens=estimate["estimated_input_tokens"] + int(extra_input_tokens),
            output_tokens=estimate["reserved_output_tokens"],
            cache_read_tokens=0,
            cache_write_tokens=0,
            cache_write_5m_tokens=0,
            cache_write_1h_tokens=0,
            thinking_tokens=0,
            cache_ttl_estimated=False,
            billing_currency=CURRENCY_USD,
            cost_rp=Decimal("0"),
            reserved_rp=Decimal("0"),
            cost_usd=Decimal("0"),
            reserved_usd=new_reserved,
            analysis_log_id=analysis_log_id,
            status="RESERVED",
        )
        db.add(log)
        db.commit()

    projected = monthly_projected + new_reserved
    return {
        "request_id": request_id,
        "tier": tier,
        "model": model,
        "billing_currency": CURRENCY_USD,
        "estimated_input_tokens": estimate["estimated_input_tokens"],
        "reserved_output_tokens": estimate["reserved_output_tokens"],
        "estimated_cost_usd": estimate["estimated_cost_usd"],
        "reserved_cost_usd": new_reserved,
        "estimated_cost_rp": Decimal("0"),
        "reserved_cost_rp": Decimal("0"),
        "projected_monthly_cost_usd": _money(projected),
        "anthropic_budget_usd": budget,
        "remaining_usd": _money(max(Decimal("0"), budget - projected)),
        "period_start": current_period_start(usage_date).isoformat(),
        "period_days": int(settings.AI_BUDGET_PERIOD_DAYS),
    }


# ============================================================
# FINALISASI
# ============================================================

def _is_usd_row(log):
    currency = getattr(log, "billing_currency", CURRENCY_IDR) or CURRENCY_IDR
    return str(currency).upper() == CURRENCY_USD


def finalize_ai_request(
    request_id,
    input_tokens,
    output_tokens,
    cache_read_tokens=0,
    cache_write_tokens=0,
    cache_write_5m_tokens=None,
    cache_write_1h_tokens=None,
    thinking_tokens=0,
    cache_ttl_known=None,
):
    """Mengganti reservasi dengan biaya aktual sesuai currency baris log."""
    _validate_tokens(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        thinking_tokens=thinking_tokens,
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
            return _money(log.cost_usd if _is_usd_row(log) else log.cost_rp)
        if log.status not in ("RESERVED", "UNKNOWN"):
            raise ValueError(f"Request tidak bisa diselesaikan: {log.status}")

        if _is_usd_row(log):
            breakdown = usd_cost_breakdown(
                model=log.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_5m_tokens=cache_write_5m_tokens or 0,
                cache_write_1h_tokens=cache_write_1h_tokens or 0,
                cache_write_tokens=cache_write_tokens or 0,
            )
            if cache_ttl_known is True:
                breakdown["cache_ttl_estimated"] = False
            cost = breakdown["cost_usd"]
            log.cost_usd = cost
            log.reserved_usd = Decimal("0")
            log.cost_rp = Decimal("0")
            log.reserved_rp = Decimal("0")
            log.cache_write_5m_tokens = breakdown["cache_write_5m_tokens"]
            log.cache_write_1h_tokens = breakdown["cache_write_1h_tokens"]
            log.cache_write_tokens = (
                breakdown["cache_write_5m_tokens"]
                + breakdown["cache_write_1h_tokens"]
            )
            log.cache_ttl_estimated = breakdown["cache_ttl_estimated"]
            log.billing_currency = CURRENCY_USD
        else:
            total_cache_write = int(cache_write_tokens or 0)
            if not total_cache_write:
                total_cache_write = int(cache_write_5m_tokens or 0) + int(
                    cache_write_1h_tokens or 0
                )
            cost = calculate_cost_rp(
                tier=log.tier,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=total_cache_write,
            )
            log.cost_rp = cost
            log.reserved_rp = Decimal("0")
            log.cache_write_tokens = total_cache_write
            log.billing_currency = getattr(log, "billing_currency", None) or CURRENCY_IDR

        log.input_tokens = input_tokens
        log.output_tokens = output_tokens
        log.cache_read_tokens = cache_read_tokens
        log.thinking_tokens = int(thinking_tokens or 0)
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
            log.reserved_usd = Decimal("0")
            log.input_tokens = 0
            log.output_tokens = 0
            log.cache_read_tokens = 0
            log.cache_write_tokens = 0
            log.cache_write_5m_tokens = 0
            log.cache_write_1h_tokens = 0
            final_status = "FAILED"
        else:
            log.status = "UNKNOWN"
            final_status = "UNKNOWN"
        db.commit()
    return final_status


# ============================================================
# ALERT THRESHOLD
# ============================================================

def record_budget_alerts(currency=CURRENCY_USD):
    """Catat alert 70/85/95 sekali per siklus budget; aman untuk request paralel."""
    currency = str(currency).upper()
    usage_date = current_usage_date()
    period_start = current_period_start(usage_date)
    budget = budget_for_currency(currency)
    if budget <= 0:
        return []

    created = []
    with SessionLocal() as db:
        _lock_budget(db)
        if currency == CURRENCY_USD:
            actual, reserved = _monthly_usd_totals(db, usage_date)
        else:
            actual, reserved = _monthly_totals(db, usage_date)
        projected = actual + reserved
        percent_used = (projected / budget * Decimal("100")).quantize(
            Decimal("0.0001")
        )

        for threshold in warning_thresholds():
            if percent_used < Decimal(threshold):
                continue
            existing = db.scalar(
                select(AIUsageAlert.id).where(
                    AIUsageAlert.billing_currency == currency,
                    AIUsageAlert.period_start == period_start,
                    AIUsageAlert.threshold_percent == threshold,
                )
            )
            if existing:
                continue
            db.add(
                AIUsageAlert(
                    billing_currency=currency,
                    period_start=period_start,
                    threshold_percent=threshold,
                    budget_amount=budget,
                    spent_amount=_money(projected),
                    percent_used=percent_used,
                )
            )
            created.append(threshold)

        try:
            db.commit()
        except IntegrityError:
            # Proses paralel sudah mencatat alert yang sama.
            db.rollback()
            created = []
    return created


def get_budget_status(currency=CURRENCY_USD):
    """Status budget ringkas untuk gating AI Router dan laporan."""
    currency = str(currency).upper()
    if currency == CURRENCY_USD:
        snapshot = get_anthropic_usage()
        return {
            "currency": CURRENCY_USD,
            "budget": snapshot["budget_usd"],
            "actual": snapshot["actual_cost_usd"],
            "reserved": snapshot["reserved_cost_usd"],
            "projected": snapshot["projected_cost_usd"],
            "percent_used": snapshot["percent_used"],
            "budget_reached": snapshot["budget_reached"],
        }
    daily = get_daily_usage()
    return {
        "currency": CURRENCY_IDR,
        "budget": daily["hard_limit_rp"],
        "actual": daily["actual_cost_rp"],
        "reserved": daily["reserved_cost_rp"],
        "projected": daily["projected_cost_rp"],
        "percent_used": Decimal("0"),
        "budget_reached": daily["hard_limit_reached"],
    }
