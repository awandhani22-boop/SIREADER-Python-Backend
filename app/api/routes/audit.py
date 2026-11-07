"""Endpoint audit approval yang disanitasi oleh database view."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_ro_session
from app.api.schemas import AuditDecisionItemDTO, AuditDecisionsResponse
from app.api.views import v_api_audit_decisions

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("/decisions", response_model=AuditDecisionsResponse)
def get_audit_decisions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    session: Session = Depends(get_ro_session),
) -> AuditDecisionsResponse:
    audit = v_api_audit_decisions
    rows = (
        session.execute(
            select(audit)
            .order_by(audit.c.decided_at.desc(), audit.c.audit_id.desc())
            .limit(limit)
            .offset(offset)
        ).mappings().all()
    )
    decisions = [AuditDecisionItemDTO(**dict(row)) for row in rows]
    return AuditDecisionsResponse(
        server_time_utc=datetime.now(timezone.utc),
        total_returned=len(decisions),
        offset=offset,
        decisions=decisions,
    )
