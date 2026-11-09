"""Read-only governance endpoints for human output review discovery and inspection."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import (
    WorkforceTaskReviewSummaryDTO,
    WorkforceTaskReviewDetailDTO,
    WorkforceTaskReviewsResponse,
)
from app.api.views import v_api_workforce_task_reviews
from app.ai.sanitizer import sanitize_review_payload

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/reviews/pending", response_model=WorkforceTaskReviewsResponse)
def get_pending_governance_reviews(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    session: Session = Depends(get_ro_session),
) -> WorkforceTaskReviewsResponse:
    table = v_api_workforce_task_reviews
    conditions = [table.c.status == "PENDING"]
    total = int(session.scalar(select(func.count()).select_from(table).where(*conditions)) or 0)
    summary_columns = [c for c in table.c if c.name not in ("original_output", "adjusted_output")]
    rows = session.execute(
        select(*summary_columns).where(*conditions)
        .order_by(table.c.review_expires_at.asc(), table.c.review_id.asc())
        .limit(limit).offset(offset)
    ).mappings().all()
    return WorkforceTaskReviewsResponse(
        server_time_utc=_now(),
        total_count=total,
        limit=limit,
        offset=offset,
        reviews=[WorkforceTaskReviewSummaryDTO(**dict(row)) for row in rows],
    )


@router.get("/reviews/{review_id}", response_model=WorkforceTaskReviewDetailDTO)
def get_governance_review_detail(
    review_id: str = Path(min_length=8, max_length=64),
    session: Session = Depends(get_ro_session),
) -> WorkforceTaskReviewDetailDTO:
    table = v_api_workforce_task_reviews
    row = session.execute(
        select(table).where(table.c.review_id == review_id)
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    data = dict(row)
    if data.get("original_output"):
        data["original_output"] = sanitize_review_payload(data["original_output"])
    if data.get("adjusted_output"):
        data["adjusted_output"] = sanitize_review_payload(data["adjusted_output"])
    return WorkforceTaskReviewDetailDTO(**data)
