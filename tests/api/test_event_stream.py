"""Unit and API checks for the bounded observability event stream."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.config import api_settings
from app.api.event_stream import (
    EventHub,
    EventStreamUnavailable,
    SubscriberLimitReached,
)
from app.api.server import create_app

SECRET = "test-only-api-secret"


def run(coro):
    return asyncio.run(coro)


def test_event_envelope_contains_only_allowlisted_resource_metadata():
    hub = EventHub()
    message = hub.publish_resources(["audit", "pipeline", "audit", "invalid"])
    assert message is not None
    assert message.event == "resources.invalidated"
    assert message.event_id == f"{hub.epoch}:1"
    assert message.data["schema_version"] == 1
    assert message.data["resources"] == ["pipeline", "audit"]
    assert set(message.data) == {"schema_version", "occurred_at", "resources"}
    assert "row" not in message.encode().lower()


def test_unknown_resource_is_ignored_without_publishing():
    hub = EventHub()
    assert hub.publish_resources(["message_text", "symbol_price"]) is None
    assert hub.latest_event_id is None


def test_listener_readiness_fails_closed():
    hub = EventHub()
    with pytest.raises(EventStreamUnavailable):
        hub.subscribe()
    hub.set_listener_ready(True)
    subscriber = hub.subscribe()
    assert hub.subscriber_count == 1
    hub.unsubscribe(subscriber)
    assert hub.subscriber_count == 0


def test_subscriber_limit_is_enforced():
    hub = EventHub(max_subscribers=1)
    hub.set_listener_ready(True)
    first = hub.subscribe()
    with pytest.raises(SubscriberLimitReached):
        hub.subscribe()
    hub.unsubscribe(first)
    assert hub.subscriber_count == 0


def test_replay_after_valid_cursor_is_ordered():
    async def scenario():
        hub = EventHub()
        hub.set_listener_ready(True)
        first = hub.publish_resources(["system"])
        hub.publish_resources(["telemetry"])
        hub.publish_resources(["audit"])
        hub.publish_resources(["market_watches"])
        subscriber = hub.subscribe(first.event_id)
        one = await subscriber.next_message(0.01)
        two = await subscriber.next_message(0.01)
        assert one is not None and one.data["resources"] == ["telemetry"]
        assert two is not None and two.data["resources"] == ["audit"]
        hub.unsubscribe(subscriber)

    run(scenario())


@pytest.mark.parametrize("cursor,reason", [
    ("not-an-event-id", "cursor_invalid"),
    (f"{uuid.uuid4()}:1", "epoch_changed"),
])
def test_invalid_or_foreign_cursor_sends_reset(cursor, reason):
    async def scenario():
        hub = EventHub()
        hub.set_listener_ready(True)
        hub.publish_resources(["system"])
        subscriber = hub.subscribe(cursor)
        message = await subscriber.next_message(0.01)
        assert message is not None
        assert message.event == "stream.reset"
        assert message.data["reason"] == reason
        hub.unsubscribe(subscriber)

    run(scenario())


def test_future_cursor_sends_reset():
    async def scenario():
        hub = EventHub()
        hub.set_listener_ready(True)
        message = hub.publish_resources(["system"])
        cursor = f"{hub.epoch}:{int(message.event_id.rsplit(':', 1)[1]) + 5}"
        subscriber = hub.subscribe(cursor)
        reset = await subscriber.next_message(0.01)
        assert reset is not None and reset.data["reason"] == "cursor_ahead"
        hub.unsubscribe(subscriber)

    run(scenario())


def test_expired_cursor_sends_replay_gap_reset():
    async def scenario():
        hub = EventHub(replay_capacity=2, subscriber_queue_capacity=4)
        hub.set_listener_ready(True)
        first = hub.publish_resources(["system"])
        hub.publish_resources(["telemetry"])
        hub.publish_resources(["audit"])
        hub.publish_resources(["market_watches"])
        subscriber = hub.subscribe(first.event_id)
        reset = await subscriber.next_message(0.01)
        assert reset is not None
        assert reset.event == "stream.reset"
        assert reset.data["reason"] == "replay_gap"
        hub.unsubscribe(subscriber)

    run(scenario())


def test_slow_subscriber_is_reset_and_removed():
    async def scenario():
        hub = EventHub(subscriber_queue_capacity=2)
        hub.set_listener_ready(True)
        subscriber = hub.subscribe()
        hub.publish_resources(["system"])
        hub.publish_resources(["telemetry"])
        hub.publish_resources(["audit"])
        assert subscriber.closed is True
        reset = await subscriber.next_message(0.01)
        assert reset is not None
        assert reset.event == "stream.reset"
        assert reset.data["reason"] == "subscriber_overflow"
        assert hub.subscriber_count == 0

    run(scenario())


def test_listener_disconnect_and_recovery_reset_existing_streams():
    async def scenario():
        hub = EventHub()
        hub.set_listener_ready(True)
        subscriber = hub.subscribe()
        hub.set_listener_ready(False)
        reset = await subscriber.next_message(0.01)
        assert reset is not None
        assert reset.data["reason"] == "listener_disconnected"
        assert hub.subscriber_count == 0
        hub.set_listener_ready(True)

    run(scenario())


def test_notification_debounce_coalesces_resource_keys():
    async def scenario():
        hub = EventHub(debounce_seconds=0.01)
        hub.set_listener_ready(True)
        subscriber = hub.subscribe()
        hub.note_resource("system")
        hub.note_resource("pipeline")
        hub.note_resource("system")
        await asyncio.sleep(0.03)
        message = await subscriber.next_message(0.01)
        assert message is not None
        assert message.data["resources"] == ["system", "pipeline"]
        hub.unsubscribe(subscriber)

    run(scenario())


def test_event_route_requires_bearer_and_listener_readiness(monkeypatch):
    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", SECRET)
    app = create_app(manage_database_lifecycle=False)
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/events/stream")
        assert unauthorized.status_code == 401
        assert client.get(
            "/api/v1/events/stream",
            headers={"Authorization": f"Bearer {SECRET}"},
        ).status_code == 503
