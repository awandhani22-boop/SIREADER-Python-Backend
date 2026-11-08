"""Authenticated SSE endpoint for resource invalidation events."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.events import (
    EventStreamCapacityExceeded,
    EventStreamUnavailable,
    ObservabilityEventHub,
)

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.get("/stream")
async def stream_observability_events(
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    hub: ObservabilityEventHub = request.app.state.observability_event_hub

    try:
        subscription = hub.subscribe(last_event_id)
    except EventStreamUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Observability event stream is unavailable.",
            headers={"Retry-After": "5"},
        )
    except EventStreamCapacityExceeded:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Observability event stream capacity is reached.",
            headers={"Retry-After": "5"},
        )

    async def event_body() -> AsyncIterator[str]:
        try:
            for event in subscription.initial_events:
                yield event.to_sse()
            if subscription.closed_after_initial:
                return

            while True:
                if await request.is_disconnected():
                    return
                event = await hub.next_event(subscription)
                if event is None:
                    yield ": ping\n\n"
                    continue
                yield event.to_sse()
                if event.name == "stream.reset":
                    return
        except asyncio.CancelledError:
            raise
        finally:
            hub.unsubscribe(subscription)

    return StreamingResponse(
        event_body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )
