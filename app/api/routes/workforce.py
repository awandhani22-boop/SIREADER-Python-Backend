"""Read-only endpoints for the persistent generic workforce runtime."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import (
    WorkforceActivityDTO, WorkforceActivityResponse,
    WorkforceAttemptsResponse, WorkforceAttemptDTO,
    WorkforceRuntimeDTO, WorkforceRuntimeResponse, WorkforceWorkerDTO,
    WorkforceTaskDTO, WorkforceTasksResponse, WorkforceTaskStatus,
    WorkforceTimelineEventDTO, WorkforceTaskSummaryDTO, WorkforceRetryLineageDTO,
    WorkforceTaskTimelineResponse,
)
from app.api.views import (
    v_api_workforce_activity, v_api_workforce_attempts,
    v_api_workforce_runtime, v_api_workforce_tasks, v_api_workforce_workers,
    v_api_workforce_task_timeline,
)

router = APIRouter(prefix="/api/v1/workforce", tags=["workforce"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/runtime", response_model=WorkforceRuntimeResponse)
def get_workforce_runtime(
    session: Session = Depends(get_ro_session),
) -> WorkforceRuntimeResponse:
    runtime_row = session.execute(select(v_api_workforce_runtime).limit(1)).mappings().one_or_none()
    if runtime_row is None:
        raise HTTPException(status_code=503, detail="Workforce runtime view is unavailable.")
    workers = session.execute(
        select(v_api_workforce_workers).order_by(v_api_workforce_workers.c.worker_id.asc())
    ).mappings().all()
    return WorkforceRuntimeResponse(
        server_time_utc=_now(),
        runtime=WorkforceRuntimeDTO(**dict(runtime_row)),
        workers=[WorkforceWorkerDTO(**dict(row)) for row in workers],
    )


@router.get("/tasks", response_model=WorkforceTasksResponse)
def get_workforce_tasks(
    status: WorkforceTaskStatus | None = None,
    worker_id: str | None = Query(default=None, min_length=2, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    session: Session = Depends(get_ro_session),
) -> WorkforceTasksResponse:
    table = v_api_workforce_tasks
    conditions = []
    if status is not None:
        conditions.append(table.c.status == status)
    if worker_id is not None:
        conditions.append(
            (table.c.assigned_worker_id == worker_id)
            | (table.c.requested_worker_id == worker_id)
        )
    total = int(session.scalar(select(func.count()).select_from(table).where(*conditions)) or 0)
    rows = session.execute(
        select(table).where(*conditions)
        .order_by(table.c.created_at.desc(), table.c.task_id.asc())
        .limit(limit).offset(offset)
    ).mappings().all()
    return WorkforceTasksResponse(
        server_time_utc=_now(),
        total_count=total,
        limit=limit,
        offset=offset,
        tasks=[WorkforceTaskDTO(**dict(row)) for row in rows],
    )


@router.get("/attempts", response_model=WorkforceAttemptsResponse)
def get_workforce_attempts(
    task_id: str = Query(min_length=8, max_length=64),
    session: Session = Depends(get_ro_session),
) -> WorkforceAttemptsResponse:
    task_exists = session.execute(
        select(v_api_workforce_tasks.c.task_id)
        .where(v_api_workforce_tasks.c.task_id == task_id)
        .limit(1)
    ).first()
    if task_exists is None:
        raise HTTPException(status_code=404, detail="Workforce task was not found.")
    rows = session.execute(
        select(v_api_workforce_attempts)
        .where(v_api_workforce_attempts.c.task_id == task_id)
        .order_by(v_api_workforce_attempts.c.attempt_number.asc())
        .limit(10)
    ).mappings().all()
    return WorkforceAttemptsResponse(
        server_time_utc=_now(),
        task_id=task_id,
        attempts=[WorkforceAttemptDTO(**dict(row)) for row in rows],
    )


@router.get("/activity", response_model=WorkforceActivityResponse)
def get_workforce_activity(
    worker_id: str | None = Query(default=None, min_length=2, max_length=64),
    task_id: str | None = Query(default=None, min_length=8, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    session: Session = Depends(get_ro_session),
) -> WorkforceActivityResponse:
    table = v_api_workforce_activity
    conditions = []
    if worker_id is not None:
        conditions.append(table.c.worker_id == worker_id)
    if task_id is not None:
        conditions.append(table.c.task_id == task_id)
    total = int(session.scalar(select(func.count()).select_from(table).where(*conditions)) or 0)
    rows = session.execute(
        select(table).where(*conditions)
        .order_by(table.c.occurred_at.desc(), table.c.event_id.asc())
        .limit(limit).offset(offset)
    ).mappings().all()
    return WorkforceActivityResponse(
        server_time_utc=_now(),
        total_count=total,
        limit=limit,
        offset=offset,
        events=[WorkforceActivityDTO(**dict(row)) for row in rows],
    )



@router.get("/tasks/{task_id}/timeline", response_model=WorkforceTaskTimelineResponse)
def get_workforce_task_timeline(
    task_id: str = Path(min_length=8, max_length=64),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    session: Session = Depends(get_ro_session),
) -> WorkforceTaskTimelineResponse:
    task_row = session.execute(
        select(v_api_workforce_tasks)
        .where(v_api_workforce_tasks.c.task_id == task_id)
        .limit(1)
    ).mappings().one_or_none()
    if task_row is None:
        raise HTTPException(status_code=404, detail="Workforce task was not found.")

    retried_by_task_id = session.scalar(
        select(v_api_workforce_tasks.c.task_id)
        .where(v_api_workforce_tasks.c.related_task_id == task_id)
        .limit(1)
    )

    table = v_api_workforce_task_timeline
    conditions = [table.c.task_id == task_id]
    total_events = int(session.scalar(select(func.count()).select_from(table).where(*conditions)) or 0)
    event_rows = session.execute(
        select(table).where(*conditions)
        .order_by(table.c.event_seq.asc())
        .limit(limit).offset(offset)
    ).mappings().all()

    task_summary = WorkforceTaskSummaryDTO(
        task_id=task_row["task_id"],
        status=task_row["status"],
        title=task_row["title"],
        task_type=task_row["task_type"],
        required_capability=task_row["required_capability"],
        assigned_worker_id=task_row["assigned_worker_id"],
        requested_worker_id=task_row["requested_worker_id"],
        priority=task_row["priority"],
        attempts=task_row["attempts"],
        max_attempts=task_row["max_attempts"],
        created_at=task_row["created_at"],
        completed_at=task_row["completed_at"],
        related_task_id=task_row["related_task_id"],
    )
    retry_lineage = WorkforceRetryLineageDTO(
        retry_of_task_id=task_row["related_task_id"],
        retried_by_task_id=retried_by_task_id,
    )
    events = [WorkforceTimelineEventDTO(**dict(r)) for r in event_rows]

    return WorkforceTaskTimelineResponse(
        server_time_utc=_now(),
        task_id=task_id,
        task=task_summary,
        retry_lineage=retry_lineage,
        total_events=total_events,
        limit=limit,
        offset=offset,
        events=events,
    )
