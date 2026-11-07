"""Endpoint pipeline read-only: record in-flight dan budget queue."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import ActivePipelineResponse, InFlightAnalysisDTO, QueuedMessageDTO
from app.api.views import v_api_active_in_flight_tasks, v_api_queued_pending_messages

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])


@router.get("/active", response_model=ActivePipelineResponse)
def get_active_pipeline(
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_ro_session),
) -> ActivePipelineResponse:
    inflight = v_api_active_in_flight_tasks
    queued = v_api_queued_pending_messages
    total_in_flight = int(session.execute(select(func.count()).select_from(inflight)).scalar_one())
    total_queued = int(session.execute(select(func.count()).select_from(queued)).scalar_one())

    inflight_rows = (
        session.execute(
            select(inflight).order_by(inflight.c.created_at.asc(), inflight.c.task_id.asc()).limit(limit)
        ).mappings().all()
    )
    queued_rows = (
        session.execute(
            select(queued).order_by(queued.c.created_at.asc(), queued.c.queue_item_id.asc()).limit(limit)
        ).mappings().all()
    )
    tasks = [
        InFlightAnalysisDTO(
            task_id=row["task_id"],
            status=row["status"],
            liveness_status="IN_FLIGHT_UNCONFIRMED",
            created_at=row["created_at"],
            elapsed_seconds=max(0.0, float(row["elapsed_seconds"] or 0)),
        )
        for row in inflight_rows
    ]
    messages = [QueuedMessageDTO(**dict(row)) for row in queued_rows]
    return ActivePipelineResponse(
        server_time_utc=datetime.now(timezone.utc),
        total_in_flight=total_in_flight,
        total_queued=total_queued,
        in_flight_tasks=tasks,
        queued_messages=messages,
    )
