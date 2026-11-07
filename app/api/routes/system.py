"""Endpoint status sistem read-only."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import RuntimeControlDTO, SystemStatusResponse
from app.api.views import v_api_system_runtime_state

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/status", response_model=SystemStatusResponse)
def get_system_status(session: Session = Depends(get_ro_session)) -> SystemStatusResponse:
    row = session.execute(select(v_api_system_runtime_state).limit(1)).mappings().first()
    runtime_control = RuntimeControlDTO(**dict(row)) if row is not None else None
    return SystemStatusResponse(
        api_status="HEALTHY",
        database_status="CONNECTED",
        server_time_utc=datetime.now(timezone.utc),
        runtime_control=runtime_control,
    )
