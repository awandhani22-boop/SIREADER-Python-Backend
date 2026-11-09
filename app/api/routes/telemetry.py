"""Endpoint agregasi telemetri biaya/token dengan pemisahan mata uang."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import (
    BudgetObservabilityDTO, BudgetTierDTO, BudgetWindowDTO,
    CurrencyTelemetryDTO, ModelTelemetryDTO, TaskCostBreakdownDTO,
    TelemetryUsageResponse, TokenBreakdownDTO, WorkerCostBreakdownDTO,
 )
from app.api.views import v_api_telemetry_usage, v_api_workforce_ai_usage
from app.config import settings

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


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.000001"))


def _warning_level(projected: Decimal, budget: Decimal, hard_limit: Decimal | None = None) -> tuple[str, int | None]:
    if hard_limit is not None and hard_limit > 0 and projected >= hard_limit:
        return "HARD_LIMIT", 100
    if budget <= 0:
        return "NONE", None
    percent = projected / budget * Decimal("100")
    if percent >= 95:
        return "CRITICAL", 95
    if percent >= 85:
        return "WARNING", 85
    if percent >= 70:
        return "SOFT", 70
    return "NONE", None


def _budget_window(
    *, currency: str, window: str, start: date, end: date,
    budget: Decimal, actual: Decimal, reserved: Decimal,
    hard_limit: Decimal | None = None, status_counts: dict[str, int] | None = None,
) -> BudgetWindowDTO:
    projected = actual + reserved
    level, threshold = _warning_level(projected, budget, hard_limit)
    percent = (
        (projected / budget * Decimal("100")).quantize(Decimal("0.0001"))
        if budget > 0 else Decimal("0")
    )
    return BudgetWindowDTO(
        currency=currency,
        window=window,
        period_start=start,
        period_end=end,
        budget_amount=budget,
        hard_limit_amount=hard_limit,
        actual_cost=actual,
        reserved_cost=reserved,
        projected_cost=projected,
        remaining_budget=max(Decimal("0"), budget - projected),
        remaining_hard_limit=(
            max(Decimal("0"), hard_limit - projected) if hard_limit is not None else None
        ),
        percent_used=percent,
        warning_threshold_percent=threshold,
        warning_level=level,
        status_counts=status_counts or {},
    )


def _budget_observability(session: Session, today: date) -> BudgetObservabilityDTO:
    usage = v_api_telemetry_usage
    daily_rows = session.execute(
        select(
            usage.c.billing_currency,
            usage.c.status,
            func.coalesce(func.sum(usage.c.cost_rp), 0).label("cost_rp"),
            func.coalesce(func.sum(usage.c.reserved_rp), 0).label("reserved_rp"),
            func.coalesce(func.sum(usage.c.cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(usage.c.reserved_usd), 0).label("reserved_usd"),
            func.count().label("requests"),
        )
        .where(usage.c.usage_date == today)
        .group_by(usage.c.billing_currency, usage.c.status)
    ).mappings().all()

    month_days = int(settings.AI_BUDGET_PERIOD_DAYS)
    if not 1 <= month_days <= 366:
        raise HTTPException(status_code=500, detail="AI budget period configuration is invalid.")
    month_start = today - timedelta(days=month_days - 1)
    month_rows = session.execute(
        select(
            usage.c.billing_currency,
            usage.c.tier,
            usage.c.status,
            func.coalesce(func.sum(usage.c.cost_rp), 0).label("cost_rp"),
            func.coalesce(func.sum(usage.c.reserved_rp), 0).label("reserved_rp"),
            func.coalesce(func.sum(usage.c.cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(usage.c.reserved_usd), 0).label("reserved_usd"),
            func.count().label("requests"),
        )
        .where(usage.c.usage_date >= month_start, usage.c.usage_date <= today)
        .group_by(usage.c.billing_currency, usage.c.tier, usage.c.status)
    ).mappings().all()

    def amount(row: Any, currency: str, field: str) -> Decimal:
        suffix = "rp" if currency == "IDR" else "usd"
        return _money(row[f"{field}_{suffix}"])

    daily_totals = {"actual": Decimal("0"), "reserved": Decimal("0")}
    daily_status: dict[str, int] = {}
    for row in daily_rows:
        if row["billing_currency"] == "IDR":
            daily_totals["actual"] += amount(row, "IDR", "cost")
            daily_totals["reserved"] += amount(row, "IDR", "reserved")
        elif row["billing_currency"] not in {"USD", "IDR"}:
            _checked_currency(row["billing_currency"])
        daily_status[str(row["status"])] = daily_status.get(str(row["status"]), 0) + int(row["requests"])

    month_totals: dict[str, dict[str, Any]] = {
        currency: {"actual": Decimal("0"), "reserved": Decimal("0"), "statuses": {}, "tiers": {}}
        for currency in ("IDR", "USD")
    }
    for row in month_rows:
        currency = _checked_currency(row["billing_currency"])
        bucket = month_totals[currency]
        bucket["actual"] += amount(row, currency, "cost")
        bucket["reserved"] += amount(row, currency, "reserved")
        status = str(row["status"])
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + int(row["requests"])
        tier = str(row["tier"])
        tier_bucket = bucket["tiers"].setdefault(tier, {"actual": Decimal("0"), "reserved": Decimal("0")})
        tier_bucket["actual"] += amount(row, currency, "cost")
        tier_bucket["reserved"] += amount(row, currency, "reserved")

    daily_idr = _budget_window(
        currency="IDR", window="DAILY", start=today, end=today,
        budget=Decimal(str(settings.AI_TARGET_DAILY_BUDGET_RP)),
        hard_limit=Decimal(str(settings.AI_HARD_DAILY_LIMIT_RP)),
        actual=daily_totals["actual"], reserved=daily_totals["reserved"],
        status_counts=daily_status,
    )
    idr_month = month_totals["IDR"]
    usd_month = month_totals["USD"]
    rolling_idr = _budget_window(
        currency="IDR", window="ROLLING_MONTH", start=month_start, end=today,
        budget=Decimal(str(settings.AI_MONTHLY_BUDGET_RP)),
        actual=idr_month["actual"], reserved=idr_month["reserved"],
        status_counts=idr_month["statuses"],
    )
    usd_budget = Decimal(str(settings.AI_ANTHROPIC_MONTHLY_BUDGET_USD))
    monthly_usd = _budget_window(
        currency="USD", window="MONTHLY", start=month_start, end=today,
        budget=usd_budget,
        actual=usd_month["actual"], reserved=usd_month["reserved"],
        status_counts=usd_month["statuses"],
    )

    tier_budgets = {
        "FAST": Decimal(str(settings.AI_FAST_MONTHLY_BUDGET_RP)),
        "REASONING": Decimal(str(settings.AI_REASONING_MONTHLY_BUDGET_RP)),
        "ESCALATION": Decimal(str(settings.AI_ESCALATION_MONTHLY_BUDGET_RP)),
    }
    idr_tiers = []
    for tier, budget in tier_budgets.items():
        values = idr_month["tiers"].get(tier, {"actual": Decimal("0"), "reserved": Decimal("0")})
        projected = values["actual"] + values["reserved"]
        idr_tiers.append(BudgetTierDTO(
            tier=tier,
            currency="IDR",
            budget_amount=budget,
            actual_cost=values["actual"],
            reserved_cost=values["reserved"],
            projected_cost=projected,
            remaining_budget=max(Decimal("0"), budget - projected),
            percent_used=(
                (projected / budget * Decimal("100")).quantize(Decimal("0.0001"))
                if budget > 0 else Decimal("0")
            ),
            budget_reached=projected >= budget,
        ))

    cost_view = v_api_workforce_ai_usage
    breakdown_rows = session.execute(
        select(
            cost_view.c.worker_id, cost_view.c.billing_currency,
            func.coalesce(func.sum(cost_view.c.cost_rp), 0).label("cost_rp"),
            func.coalesce(func.sum(cost_view.c.reserved_rp), 0).label("reserved_rp"),
            func.coalesce(func.sum(cost_view.c.cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(cost_view.c.reserved_usd), 0).label("reserved_usd"),
            func.count().label("requests"),
        )
        .where(cost_view.c.usage_date >= month_start, cost_view.c.usage_date <= today)
        .group_by(cost_view.c.worker_id, cost_view.c.billing_currency)
        .order_by(cost_view.c.worker_id.asc(), cost_view.c.billing_currency.asc())
    ).mappings().all()
    worker_breakdown = []
    for row in breakdown_rows:
        currency = _checked_currency(row["billing_currency"])
        actual = amount(row, currency, "cost")
        reserved = amount(row, currency, "reserved")
        worker_breakdown.append(WorkerCostBreakdownDTO(
            worker_id=str(row["worker_id"]), currency=currency,
            actual_cost=actual, reserved_cost=reserved,
            projected_cost=actual + reserved, requests=int(row["requests"]),
        ))

    task_rows = session.execute(
        select(
            cost_view.c.task_id, cost_view.c.worker_id, cost_view.c.tier,
            cost_view.c.model, cost_view.c.billing_currency,
            func.coalesce(func.sum(cost_view.c.cost_rp), 0).label("cost_rp"),
            func.coalesce(func.sum(cost_view.c.reserved_rp), 0).label("reserved_rp"),
            func.coalesce(func.sum(cost_view.c.cost_usd), 0).label("cost_usd"),
            func.coalesce(func.sum(cost_view.c.reserved_usd), 0).label("reserved_usd"),
            func.count().label("requests"),
        )
        .where(cost_view.c.usage_date >= month_start, cost_view.c.usage_date <= today)
        .group_by(
            cost_view.c.task_id, cost_view.c.worker_id, cost_view.c.tier,
            cost_view.c.model, cost_view.c.billing_currency,
        )
        .order_by(func.sum(cost_view.c.cost_rp + cost_view.c.reserved_rp + cost_view.c.cost_usd + cost_view.c.reserved_usd).desc())
        .limit(25)
    ).mappings().all()
    recent_task_costs = []
    for row in task_rows:
        currency = _checked_currency(row["billing_currency"])
        actual = amount(row, currency, "cost")
        reserved = amount(row, currency, "reserved")
        recent_task_costs.append(TaskCostBreakdownDTO(
            task_id=str(row["task_id"]), worker_id=str(row["worker_id"]),
            tier=str(row["tier"]), model=str(row["model"]), currency=currency,
            actual_cost=actual, reserved_cost=reserved,
            projected_cost=actual + reserved, requests=int(row["requests"]),
        ))
    return BudgetObservabilityDTO(
        daily_idr=daily_idr, rolling_month_idr=rolling_idr,
        monthly_anthropic_usd=monthly_usd, idr_tiers=idr_tiers,
        worker_breakdown=worker_breakdown, recent_task_costs=recent_task_costs,
    )


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
        budget=_budget_observability(session, local_today),
    )
