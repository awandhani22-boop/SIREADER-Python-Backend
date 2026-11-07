"""Endpoint agregasi telemetri biaya/token dengan pemisahan mata uang."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import CurrencyTelemetryDTO, ModelTelemetryDTO, TelemetryUsageResponse, TokenBreakdownDTO
from app.api.views import v_api_telemetry_usage

router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])
JAKARTA = ZoneInfo("Asia/Jakarta")


def _currency_sum(column_idr: Any, column_usd: Any) -> Any:
    currency = v_api_telemetry_usage.c.billing_currency
    return case(
        (currency == "IDR", func.coalesce(func.sum(column_idr), 0)),
        (currency == "USD", func.coalesce(func.sum(column_usd), 0)),
        else_=None,
    )


def _tokens(row: Any) -> TokenBreakdownDTO:
    return TokenBreakdownDTO(
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cache_read_tokens=int(row["cache_read_tokens"] or 0),
        cache_write_tokens=int(row["cache_write_tokens"] or 0),
        thinking_tokens=int(row["thinking_tokens"] or 0),
    )


def _checked_currency(value: str | None) -> str:
    if value not in {"IDR", "USD"}:
        raise HTTPException(status_code=500, detail="Unsupported billing currency in telemetry data.")
    return value


@router.get("/usage", response_model=TelemetryUsageResponse)
def get_telemetry_usage(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    session: Session = Depends(get_ro_session),
) -> TelemetryUsageResponse:
    local_today = datetime.now(JAKARTA).date()
    resolved_end = end_date or local_today
    resolved_start = start_date or resolved_end - timedelta(days=6)
    if resolved_start > resolved_end:
        raise HTTPException(status_code=422, detail="start_date must not exceed end_date.")
    if (resolved_end - resolved_start).days > 365:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 366 days.")

    usage = v_api_telemetry_usage
    date_filter = (
        usage.c.usage_date >= resolved_start,
        usage.c.usage_date <= resolved_end,
    )
    totals_stmt = (
        select(
            usage.c.billing_currency,
            _currency_sum(usage.c.cost_rp, usage.c.cost_usd).label("total_cost"),
            _currency_sum(usage.c.reserved_rp, usage.c.reserved_usd).label("total_reserved"),
            func.coalesce(func.sum(usage.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(usage.c.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(usage.c.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(usage.c.cache_write_tokens), 0).label("cache_write_tokens"),
            func.coalesce(func.sum(usage.c.thinking_tokens), 0).label("thinking_tokens"),
            func.count().label("total_requests"),
        ).where(*date_filter).group_by(usage.c.billing_currency).order_by(usage.c.billing_currency)
    )
    status_stmt = (
        select(usage.c.billing_currency, usage.c.status, func.count().label("status_count"))
        .where(*date_filter)
        .group_by(usage.c.billing_currency, usage.c.status)
    )
    model_stmt = (
        select(
            usage.c.billing_currency,
            usage.c.model,
            usage.c.tier,
            func.count().label("requests"),
            _currency_sum(usage.c.cost_rp, usage.c.cost_usd).label("cost"),
            _currency_sum(usage.c.reserved_rp, usage.c.reserved_usd).label("reserved"),
            func.coalesce(func.sum(usage.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(usage.c.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(usage.c.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(usage.c.cache_write_tokens), 0).label("cache_write_tokens"),
            func.coalesce(func.sum(usage.c.thinking_tokens), 0).label("thinking_tokens"),
        )
        .where(*date_filter)
        .group_by(usage.c.billing_currency, usage.c.model, usage.c.tier)
        .order_by(func.count().desc(), usage.c.model.asc(), usage.c.tier.asc())
    )

    total_rows = session.execute(totals_stmt).mappings().all()
    status_rows = session.execute(status_stmt).mappings().all()
    model_rows = session.execute(model_stmt).mappings().all()

    status_counts: dict[str, dict[str, int]] = {}
    for row in status_rows:
        currency = _checked_currency(row["billing_currency"])
        status_counts.setdefault(currency, {})[str(row["status"])] = int(row["status_count"])

    currencies = []
    for row in total_rows:
        currency = _checked_currency(row["billing_currency"])
        cost = Decimal(str(row["total_cost"] or 0))
        reserved = Decimal(str(row["total_reserved"] or 0))
        currencies.append(CurrencyTelemetryDTO(
            currency=currency,
            total_cost=cost,
            total_reserved=reserved,
            total_projected=cost + reserved,
            total_requests=int(row["total_requests"]),
            tokens=_tokens(row),
            status_counts=status_counts.get(currency, {}),
        ))

    models = []
    for row in model_rows:
        currency = _checked_currency(row["billing_currency"])
        cost = Decimal(str(row["cost"] or 0))
        reserved = Decimal(str(row["reserved"] or 0))
        models.append(ModelTelemetryDTO(
            model=str(row["model"]),
            tier=str(row["tier"]),
            currency=currency,
            requests=int(row["requests"]),
            cost=cost,
            reserved=reserved,
            projected=cost + reserved,
            tokens=_tokens(row),
        ))

    return TelemetryUsageResponse(
        period_start=resolved_start,
        period_end=resolved_end,
        period_days=(resolved_end - resolved_start).days + 1,
        currencies=currencies,
        models=models,
    )
