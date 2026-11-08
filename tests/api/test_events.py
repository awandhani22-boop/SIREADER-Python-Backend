"""Unit and endpoint tests for the bounded observability event stream."""
from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.events import (
    EventStreamCapacityExceeded,
    EventStreamUnavailable,
    ObservabilityEventHub,
    parse_event_cursor,
)
from app.api.dependencies import verify_bearer_token
from app.api.server import create_app

SECRET = "test-only-event-stream-secret"


def _ready_hub(**kwargs) -> ObservabilityEventHub:
    hub = ObservabilityEventHub(**kwargs)
    hub.mark_listener_ready()
    return hub


def test_cursor_parser_accepts_exact_epoch_sequence():
    epoch = str(uuid4())
    assert parse_event_cursor(f"{epoch}:12") == (epoch, 12)
    assert parse_event_cursor("x" * 65) is None
    assert parse_event_cursor("not-an-event-id") is None
    assert parse_event_cursor(f"{epoch}:0") is None
    assert parse_event_cursor(f"{epoch}:01") is None


def test_event_payload_contains_only_allowlisted_resource_names():
    hub = _ready_hub()
    event = hub.publish_resources(["pipeline", "telemetry", "pipeline", "unknown"])
    assert event is not None
    assert event.name == "resources.invalidated"
    assert event.data["resources"] == ["telemetry", "pipeline"]
    assert set(event.data) == {"schema_version", "occurred_at", "resources"}
    assert "id" not in event.data and "payload" not in event.data


def test_unknown_notification_payload_is_ignored():
    hub = _ready_hub()
    before = hub.latest_event_id
    assert hub.publish_notification('{"resource":"pipeline","secret":"no"}') is None
    assert hub.latest_event_id == before


def test_subscriber_receives_new_event_and_unsubscribes():
    async def run():
        hub = _ready_hub()
        sub = hub.subscribe(None)
        hub.publish_resources(["audit"])
        event = await hub.next_event(sub, timeout_seconds=0.1)
        assert event is not None
        assert event.name == "resources.invalidated"
        assert event.data["resources"] == ["audit"]
        hub.unsubscribe(sub)
        assert hub.subscriber_count == 0
    asyncio.run(run())


def test_same_epoch_cursor_replays_only_events_after_cursor():
    hub = _ready_hub()
    first = hub.publish_resources(["system"])
    second = hub.publish_resources(["pipeline"])
    assert first is not None and second is not None
    sub = hub.subscribe(first.event_id)
    assert [event.event_id for event in sub.initial_events] == [second.event_id]
    assert sub.initial_events[0].data["resources"] == ["pipeline"]
    hub.unsubscribe(sub)


@pytest.mark.parametrize("cursor", ["malformed", f"{uuid4()}:99", f"{uuid4()}:1"])
def test_invalid_future_or_other_epoch_cursor_requires_reset(cursor):
    hub = _ready_hub()
    sub = hub.subscribe(cursor)
    assert len(sub.initial_events) == 1
    assert sub.initial_events[0].name == "stream.reset"
    assert sub.closed_after_initial is True


def test_cursor_older_than_replay_ring_requires_reset():
    hub = _ready_hub(replay_capacity=2)
    old = hub.latest_event_id
    assert old is not None
    hub.publish_resources(["system"])
    hub.publish_resources(["pipeline"])
    hub.publish_resources(["audit"])
    sub = hub.subscribe(old)
    assert len(sub.initial_events) == 1
    assert sub.initial_events[0].data["reason"] == "cursor_expired"


def test_subscriber_queue_overflow_replaces_pending_events_with_reset():
    async def run():
        hub = _ready_hub(subscriber_queue_capacity=1)
        sub = hub.subscribe(None)
        hub.publish_resources(["system"])
        hub.publish_resources(["audit"])
        event = await hub.next_event(sub, timeout_seconds=0.1)
        assert event is not None
        assert event.name == "stream.reset"
        assert event.data["reason"] == "subscriber_overflow"
        assert hub.subscriber_count == 0
    asyncio.run(run())


def test_subscriber_limit_is_bounded():
    hub = _ready_hub(max_subscribers=1)
    first = hub.subscribe(None)
    with pytest.raises(EventStreamCapacityExceeded):
        hub.subscribe(None)
    hub.unsubscribe(first)
    assert hub.subscriber_count == 0


def test_stream_unavailable_when_listener_is_not_ready():
    hub = ObservabilityEventHub()
    with pytest.raises(EventStreamUnavailable):
        hub.subscribe(None)


def test_sse_endpoint_requires_bearer_and_returns_sanitized_503_when_listener_unavailable(monkeypatch):
    from app.api.config import api_settings

    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", SECRET)
    hub = ObservabilityEventHub()
    app = create_app(manage_database_lifecycle=False, event_hub=hub)
    with TestClient(app) as client:
        denied = client.get("/api/v1/events/stream")
        assert denied.status_code == 401
        unavailable = client.get(
            "/api/v1/events/stream",
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "Observability event stream is unavailable."}
        assert "text/event-stream" not in unavailable.headers.get("content-type", "")


def test_sse_endpoint_streams_event_and_heartbeat_headers(monkeypatch):
    from app.api.config import api_settings

    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", SECRET)
    hub = _ready_hub()
    app = create_app(manage_database_lifecycle=False, event_hub=hub)
    with TestClient(app) as client:
        with client.stream(
            "GET",
            "/api/v1/events/stream",
            headers={"Authorization": f"Bearer {SECRET}"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache, no-transform"
            assert response.headers["x-accel-buffering"] == "no"
            # EventSource clients receive an initial 15-second heartbeat. In tests,
            # inject an event in a separate task after the stream has subscribed.
            hub.publish_resources(["pipeline"])
            chunks = response.iter_text()
            first = next(chunks)
            assert "event: resources.invalidated" in first
            assert '"resources":["pipeline"]' in first
