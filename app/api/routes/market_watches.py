"""Endpoint observabilitas spread watches yang masih aktif."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import ActiveMarketWatchesResponse, MarketWatchItemDTO
from app.api.views import v_api_active_market_watches

router = APIRouter(prefix="/api/v1/market-watches", tags=["market-watches"])


@router.get("/active", response_model=ActiveMarketWatchesResponse)
def get_active_market_watches(
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_ro_session),
) -> ActiveMarketWatchesResponse:
    watches = v_api_active_market_watches
    total = int(session.execute(select(func.count()).select_from(watches)).scalar_one())
    rows = (
        session.execute(
            select(watches).order_by(watches.c.next_check_at.asc(), watches.c.watch_id.asc()).limit(limit)
        ).mappings().all()
    )
    return ActiveMarketWatchesResponse(
        server_time_utc=datetime.now(timezone.utc),
        total_active_watches=total,
        watches=[MarketWatchItemDTO(**dict(row)) for row in rows],
    )
