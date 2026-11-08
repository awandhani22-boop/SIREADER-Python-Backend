"""Authenticated, bounded PostgreSQL LISTEN/NOTIFY event stream support.

The five snapshot endpoints remain authoritative. Notifications carry only
allowlisted resource keys and are used solely to invalidate browser snapshots.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import random
import re
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

CHANNEL = "sireader_observability_v1"
RESOURCE_ORDER = ("system", "telemetry", "pipeline", "market_watches", "audit")
RESOURCE_KEYS = frozenset(RESOURCE_ORDER)
RESET_REASONS = frozenset({
    "cursor_invalid",
    "epoch_changed",
    "cursor_ahead",
    "replay_gap",
    "subscriber_overflow",
    "listener_disconnected",
    "listener_recovered",
    "server_shutdown",
})
EVENT_ID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([1-9][0-9]{0,18})$")


class EventStreamUnavailable(RuntimeError):
    """The PostgreSQL listener is not ready for a new stream."""


class SubscriberLimitReached(RuntimeError):
    """The bounded subscriber limit has been reached."""


@dataclass(frozen=True)
class StreamMessage:
    event: str
    data: dict[str, object]
    event_id: str | None = None

    def encode(self) -> str:
        lines: list[str] = []
        if self.event_id is not None:
            lines.append(f"id: {self.event_id}")
        lines.append(f"event: {self.event}")
        lines.append("data: " + json.dumps(self.data, separators=(",", ":"), ensure_ascii=True))
        return "\n".join(lines) + "\n\n"


class StreamSubscriber:
    """One bounded async queue. A slow consumer is reset and disconnected."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._queue: deque[StreamMessage] = deque()
        self._available = asyncio.Event()
        self.closed = False

    def offer(self, message: StreamMessage) -> None:
        if self.closed:
            return
        if len(self._queue) >= self.capacity:
            self._queue.clear()
            self._queue.append(EventHub.reset_message("subscriber_overflow"))
            self.closed = True
        else:
            self._queue.append(message)
        self._available.set()

    def reset(self, reason: str) -> None:
        if reason not in RESET_REASONS:
            reason = "server_shutdown"
        self._queue.clear()
        self._queue.append(EventHub.reset_message(reason))
        self.closed = True
        self._available.set()

    async def next_message(self, timeout: float) -> StreamMessage | None:
        if not self._queue and not self.closed:
            try:
                await asyncio.wait_for(self._available.wait(), timeout=timeout)
            except TimeoutError:
                return None
        if self._queue:
            message = self._queue.popleft()
            if not self._queue:
                self._available.clear()
            return message
        return None


class EventHub:
    """Single-process bounded event ring and subscriber fan-out."""

    def __init__(
        self,
        *,
        replay_capacity: int = 512,
        subscriber_queue_capacity: int = 64,
        max_subscribers: int = 4,
        debounce_seconds: float = 0.1,
    ) -> None:
        if replay_capacity < 1 or subscriber_queue_capacity < 1 or max_subscribers < 1:
            raise ValueError("EventHub capacities must be positive.")
        self.epoch = str(uuid4())
        self._sequence = 0
        self._events: deque[StreamMessage] = deque(maxlen=replay_capacity)
        self._subscriber_queue_capacity = subscriber_queue_capacity
        self._max_subscribers = max_subscribers
        self._subscribers: set[StreamSubscriber] = set()
        self._listener_ready = False
        self._ever_ready = False
        self._pending_resources: set[str] = set()
        self._debounce_seconds = debounce_seconds
        self._flush_handle: asyncio.TimerHandle | None = None
        self._closed = False

    @property
    def listener_ready(self) -> bool:
        return self._listener_ready and not self._closed

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def latest_event_id(self) -> str | None:
        if self._sequence == 0:
            return None
        return f"{self.epoch}:{self._sequence}"

    @staticmethod
    def reset_message(reason: str) -> StreamMessage:
        if reason not in RESET_REASONS:
            reason = "server_shutdown"
        return StreamMessage(
            event="stream.reset",
            data={"schema_version": 1, "reason": reason},
        )

    def set_listener_ready(self, ready: bool) -> None:
        if self._closed:
            return
        was_ready = self._listener_ready
        self._listener_ready = ready
        if was_ready and not ready:
            self.broadcast_reset("listener_disconnected")
        elif ready and not self._ever_ready:
            self._ever_ready = True
        elif ready and self._ever_ready and not was_ready:
            self.broadcast_reset("listener_recovered")

    def publish_resources(self, resources: list[str] | tuple[str, ...] | set[str]) -> StreamMessage | None:
        allowed = {resource for resource in resources if resource in RESOURCE_KEYS}
        if not allowed or self._closed:
            return None
        ordered = [resource for resource in RESOURCE_ORDER if resource in allowed]
        self._sequence += 1
        occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        message = StreamMessage(
            event="resources.invalidated",
            event_id=f"{self.epoch}:{self._sequence}",
            data={
                "schema_version": 1,
                "occurred_at": occurred_at,
                "resources": ordered,
            },
        )
        self._events.append(message)
        for subscriber in tuple(self._subscribers):
            subscriber.offer(message)
            if subscriber.closed:
                self._subscribers.discard(subscriber)
        return message

    def note_resource(self, resource: str) -> None:
        """Coalesce DB notifications for a short window without retaining row data."""
        if resource not in RESOURCE_KEYS or self._closed:
            if resource not in RESOURCE_KEYS:
                logger.warning("SSE_NOTIFICATION_REJECTED code=UNKNOWN_RESOURCE")
            return
        self._pending_resources.add(resource)
        if self._flush_handle is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._flush_pending()
                return
            self._flush_handle = loop.call_later(self._debounce_seconds, self._flush_pending)

    def _flush_pending(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        resources = set(self._pending_resources)
        self._pending_resources.clear()
        if resources and self.listener_ready:
            self.publish_resources(resources)

    def subscribe(self, last_event_id: str | None = None) -> StreamSubscriber:
        if not self.listener_ready:
            raise EventStreamUnavailable("Event listener is not ready.")
        if len(self._subscribers) >= self._max_subscribers:
            raise SubscriberLimitReached("Event subscriber capacity is full.")

        subscriber = StreamSubscriber(self._subscriber_queue_capacity)
        if last_event_id:
            reset_reason, replay = self._resolve_cursor(last_event_id)
            if reset_reason is not None:
                subscriber.reset(reset_reason)
            elif len(replay) > self._subscriber_queue_capacity:
                subscriber.reset("replay_gap")
            else:
                for message in replay:
                    subscriber.offer(message)
        self._subscribers.add(subscriber)
        return subscriber

    def _resolve_cursor(
        self,
        value: str,
    ) -> tuple[str | None, list[StreamMessage]]:
        if len(value) > 80:
            return "cursor_invalid", []
        match = EVENT_ID_RE.fullmatch(value)
        if match is None:
            return "cursor_invalid", []
        epoch, raw_sequence = match.groups()
        try:
            if str(UUID(epoch)) != epoch:
                return "cursor_invalid", []
        except ValueError:
            return "cursor_invalid", []
        sequence = int(raw_sequence)
        if epoch != self.epoch:
            return "epoch_changed", []
        if sequence > self._sequence:
            return "cursor_ahead", []
        if sequence == self._sequence:
            return None, []
        if not self._events:
            return "replay_gap", []
        oldest = int((self._events[0].event_id or "").rsplit(":", 1)[-1])
        if sequence < oldest - 1:
            return "replay_gap", []
        replay = [
            item for item in self._events
            if int((item.event_id or "").rsplit(":", 1)[-1]) > sequence
        ]
        return None, replay

    def unsubscribe(self, subscriber: StreamSubscriber) -> None:
        self._subscribers.discard(subscriber)
        subscriber.closed = True
        subscriber._queue.clear()
        subscriber._available.set()

    def broadcast_reset(self, reason: str) -> None:
        for subscriber in tuple(self._subscribers):
            subscriber.reset(reason)
        self._subscribers.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._listener_ready = False
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._pending_resources.clear()
        self.broadcast_reset("server_shutdown")


class PostgresNotifyListener:
    """Dedicated Psycopg async LISTEN connection using the existing RO URL."""

    def __init__(
        self,
        hub: EventHub,
        database_url_provider: Callable[[], str],
    ) -> None:
        self._hub = hub
        self._database_url_provider = database_url_provider
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="sireader-observability-listener")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._hub.set_listener_ready(False)
        self._hub.close()

    def _psycopg_dsn(self) -> str:
        raw = self._database_url_provider()
        url = make_url(raw)
        if url.get_backend_name() != "postgresql":
            raise RuntimeError("SSE listener requires the configured PostgreSQL API URL.")
        return url.set(drivername="postgresql").render_as_string(hide_password=False)

    async def _run(self) -> None:
        from psycopg import AsyncConnection

        delay = 1.0
        while True:
            connection = None
            try:
                connection = await AsyncConnection.connect(
                    self._psycopg_dsn(),
                    autocommit=True,
                    connect_timeout=5,
                    application_name="sireader-observability-listener",
                    options="-c default_transaction_read_only=on -c search_path=public",
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=3,
                )
                await connection.execute("LISTEN sireader_observability_v1")
                self._hub.set_listener_ready(True)
                delay = 1.0
                async for notification in connection.notifies():
                    self._hub.note_resource(notification.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("SSE_LISTENER_RETRY error_type=%s", type(exc).__name__)
            finally:
                self._hub.set_listener_ready(False)
                if connection is not None:
                    try:
                        await asyncio.wait_for(connection.close(), timeout=2.0)
                    except Exception:
                        pass
            await asyncio.sleep(min(30.0, delay) * random.uniform(0.8, 1.2))
            delay = min(30.0, delay * 2.0)
