"""Authenticated read-only SSE endpoint for resource invalidations."""
import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.event_stream import (
    EventHub,
    EventStreamUnavailable,
    SubscriberLimitReached,
    StreamSubscriber,
)

router = APIRouter(tags=["events"])
HEARTBEAT_SECONDS = 15.0


async def generate_event_stream(
    request: Request,
    hub: EventHub,
    subscriber: StreamSubscriber,
) -> AsyncIterator[str]:
    """Yield the retry directive, event frames, and idle heartbeat comments."""
    try:
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            message = await subscriber.next_message(HEARTBEAT_SECONDS)
            if message is None:
                if subscriber.closed:
                    break
                yield ": ping\n\n"
                continue
            yield message.encode()
            if message.event == "stream.reset":
                break
    except asyncio.CancelledError:
        raise
    finally:
        hub.unsubscribe(subscriber)


@router.get("/api/v1/events/stream")
async def stream_observability_events(request: Request):
    hub: EventHub = request.app.state.event_hub
    if not hub.listener_ready:
        return JSONResponse(
            status_code=503,
            content={"detail": "Observability event stream is unavailable."},
            headers={"Cache-Control": "no-store"},
        )

    try:
        subscriber = hub.subscribe(request.headers.get("last-event-id"))
    except EventStreamUnavailable:
        return JSONResponse(
            status_code=503,
            content={"detail": "Observability event stream is unavailable."},
            headers={"Cache-Control": "no-store"},
        )
    except SubscriberLimitReached:
        return JSONResponse(
            status_code=503,
            content={"detail": "Observability event stream capacity is full."},
            headers={"Cache-Control": "no-store", "Retry-After": "3"},
        )

    return StreamingResponse(
        generate_event_stream(request, hub, subscriber),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )
