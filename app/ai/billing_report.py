"""Laporan biaya AI: IDR dan USD SELALU dipisah.

Tidak ada penjumlahan lintas mata uang. Semua nilai uang memakai Decimal.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select

from app.config import settings
from app.ai.cost_guard import (
    CURRENCY_IDR,
    CURRENCY_USD,
    _money,
    current_period_start,
    current_usage_date,
    get_anthropic_usage,
    get_monthly_usage,
)
from app.database.connection import SessionLocal
from app.database.models import AIUsageLog


def _currency_filter(currency):
    if str(currency).upper() == CURRENCY_USD:
        return AIUsageLog.billing_currency == CURRENCY_USD
    return or_(
        AIUsageLog.billing_currency == CURRENCY_IDR,
        AIUsageLog.billing_currency.is_(None),
    )


def _cost_columns(currency):
    if str(currency).upper() == CURRENCY_USD:
        return AIUsageLog.cost_usd, AIUsageLog.reserved_usd
    return AIUsageLog.cost_rp, AIUsageLog.reserved_rp


def get_currency_summary(currency, period_days=None):
    """Ringkasan aktual, reservasi, dan status untuk satu mata uang."""
    currency = str(currency).upper()
    usage_date = current_usage_date()
    period_days = int(period_days or settings.AI_BUDGET_PERIOD_DAYS)
    start = usage_date - timedelta(days=period_days - 1)

    cost_col, reserved_col = _cost_columns(currency)
    base = [
        AIUsageLog.usage_date >= start,
        AIUsageLog.usage_date <= usage_date,
        _currency_filter(currency),
    ]

    with SessionLocal() as db:
        actual = Decimal(
            str(db.scalar(select(func.coalesce(func.sum(cost_col), 0)).where(*base)) or 0)
        )
        reserved = Decimal(
            str(
                db.scalar(
                    select(func.coalesce(func.sum(reserved_col), 0)).where(*base)
                )
                or 0
            )
        )
        status_rows = db.execute(
            select(AIUsageLog.status, func.count(AIUsageLog.id))
            .where(*base)
            .group_by(AIUsageLog.status)
        ).all()
        token_rows = db.execute(
            select(
                func.coalesce(func.sum(AIUsageLog.input_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.output_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.cache_read_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.cache_write_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.thinking_tokens), 0),
            ).where(*base)
        ).one()

    return {
        "currency": currency,
        "period_start": start.isoformat(),
        "period_end": usage_date.isoformat(),
        "period_days": period_days,
        "actual_cost": _money(actual),
        "reserved_cost": _money(reserved),
        "projected_cost": _money(actual + reserved),
        "status_counts": {status: int(count) for status, count in status_rows},
        "tokens": {
            "input": int(token_rows[0] or 0),
            "output": int(token_rows[1] or 0),
            "cache_read": int(token_rows[2] or 0),
            "cache_write": int(token_rows[3] or 0),
            "thinking": int(token_rows[4] or 0),
        },
    }


def get_model_report(currency=CURRENCY_USD, period_days=None):
    """Pemakaian per model, dengan currency terpisah."""
    currency = str(currency).upper()
    cost_col, reserved_col = _cost_columns(currency)
    usage_date = current_usage_date()
    period_days = int(period_days or settings.AI_BUDGET_PERIOD_DAYS)
    start = usage_date - timedelta(days=period_days - 1)

    with SessionLocal() as db:
        rows = db.execute(
            select(
                AIUsageLog.model,
                AIUsageLog.tier,
                func.count(AIUsageLog.id),
                func.coalesce(func.sum(cost_col), 0),
                func.coalesce(func.sum(reserved_col), 0),
                func.coalesce(func.sum(AIUsageLog.input_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.output_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.cache_read_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.cache_write_tokens), 0),
                func.coalesce(func.sum(AIUsageLog.thinking_tokens), 0),
            )
            .where(
                AIUsageLog.usage_date >= start,
                AIUsageLog.usage_date <= usage_date,
                _currency_filter(currency),
            )
            .group_by(AIUsageLog.model, AIUsageLog.tier)
            .order_by(func.coalesce(func.sum(cost_col), 0).desc())
        ).all()

    return {
        "currency": currency,
        "period_start": start.isoformat(),
        "period_end": usage_date.isoformat(),
        "models": [
            {
                "model": row[0],
                "tier": row[1],
                "requests": int(row[2]),
                "actual_cost": _money(row[3]),
                "reserved_cost": _money(row[4]),
                "tokens": {
                    "input": int(row[5]),
                    "output": int(row[6]),
                    "cache_read": int(row[7]),
                    "cache_write": int(row[8]),
                    "thinking": int(row[9]),
                },
            }
            for row in rows
        ],
    }


def get_analysis_report(currency=CURRENCY_USD, period_days=None, limit=50):
    """Biaya per analysis_log_id. Baris lama tanpa tautan masuk grup unlinked."""
    currency = str(currency).upper()
    cost_col, reserved_col = _cost_columns(currency)
    usage_date = current_usage_date()
    period_days = int(period_days or settings.AI_BUDGET_PERIOD_DAYS)
    start = usage_date - timedelta(days=period_days - 1)

    with SessionLocal() as db:
        rows = db.execute(
            select(
                AIUsageLog.analysis_log_id,
                func.count(AIUsageLog.id),
                func.coalesce(func.sum(cost_col), 0),
                func.coalesce(func.sum(reserved_col), 0),
                func.min(AIUsageLog.model),
                func.max(AIUsageLog.model),
            )
            .where(
                AIUsageLog.usage_date >= start,
                AIUsageLog.usage_date <= usage_date,
                _currency_filter(currency),
            )
            .group_by(AIUsageLog.analysis_log_id)
            .order_by(func.coalesce(func.sum(cost_col), 0).desc())
            .limit(int(limit))
        ).all()

    return {
        "currency": currency,
        "period_start": start.isoformat(),
        "period_end": usage_date.isoformat(),
        "analyses": [
            {
                "analysis_log_id": row[0],
                "linked": row[0] is not None,
                "requests": int(row[1]),
                "actual_cost": _money(row[2]),
                "reserved_cost": _money(row[3]),
                "models": sorted({m for m in (row[4], row[5]) if m}),
            }
            for row in rows
        ],
    }


def get_projection_report(period_days=None):
    """Proyeksi 30 hari dari laju pemakaian aktual. Estimasi, bukan tagihan."""
    usage_date = current_usage_date()
    period_days = int(period_days or settings.AI_BUDGET_PERIOD_DAYS)
    start = usage_date - timedelta(days=period_days - 1)

    result = {}
    for currency in (CURRENCY_IDR, CURRENCY_USD):
        cost_col, _ = _cost_columns(currency)
        with SessionLocal() as db:
            actual = Decimal(
                str(
                    db.scalar(
                        select(func.coalesce(func.sum(cost_col), 0)).where(
                            AIUsageLog.usage_date >= start,
                            AIUsageLog.usage_date <= usage_date,
                            AIUsageLog.status == "COMPLETED",
                            _currency_filter(currency),
                        )
                    )
                    or 0
                )
            )
            first_date = db.scalar(
                select(func.min(AIUsageLog.usage_date)).where(
                    AIUsageLog.usage_date >= start,
                    AIUsageLog.usage_date <= usage_date,
                    AIUsageLog.status == "COMPLETED",
                    _currency_filter(currency),
                )
            )

        if first_date is None:
            days_observed = 0
        else:
            days_observed = (usage_date - first_date).days + 1
        days_observed = max(days_observed, 0)

        if days_observed == 0:
            daily_rate = Decimal("0")
            projected = Decimal("0")
            confidence = "NO_DATA"
        else:
            daily_rate = actual / Decimal(days_observed)
            projected = daily_rate * Decimal(period_days)
            confidence = "LOW" if days_observed < 7 else "MEDIUM"

        result[currency] = {
            "currency": currency,
            "days_observed": days_observed,
            "actual_cost": _money(actual),
            "daily_rate": _money(daily_rate),
            "projected_cost_30d": _money(projected),
            "confidence": confidence,
        }

    result["budget"] = {
        CURRENCY_IDR: _money(settings.AI_MONTHLY_BUDGET_RP),
        CURRENCY_USD: _money(settings.AI_ANTHROPIC_MONTHLY_BUDGET_USD),
    }
    return result


def get_full_report(period_days=None):
    """Laporan lengkap; IDR dan USD tidak pernah dijumlahkan."""
    return {
        "idr": {
            "monthly": get_monthly_usage(),
            "summary": get_currency_summary(CURRENCY_IDR, period_days),
            "by_model": get_model_report(CURRENCY_IDR, period_days),
            "by_analysis": get_analysis_report(CURRENCY_IDR, period_days),
        },
        "usd": {
            "monthly": get_anthropic_usage(),
            "summary": get_currency_summary(CURRENCY_USD, period_days),
            "by_model": get_model_report(CURRENCY_USD, period_days),
            "by_analysis": get_analysis_report(CURRENCY_USD, period_days),
        },
        "projection": get_projection_report(period_days),
        "period_start": current_period_start().isoformat(),
    }
