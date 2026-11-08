"""Bounded in-process event fan-out for read-only observability SSE.

PostgreSQL notifications carry only allowlisted resource keys. Snapshot GET
endpoints remain authoritative; this module never publishes business-row data.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import random
import re
from typing import AsyncIterator, Iterable
from uuid import UUID, uuid4

import psycopg

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "sireader_observability_v1"
ALLOWED_RESOURCES = frozenset({
    "system",
    "telemetry",
    "pipeline",
    "market_watches",
    "audit",
})
RESOURCE_ORDER = ("system", "telemetry", "pipeline", "market_watches", "audit")
_EVENT_ID_RE = re.compile(r"^([0-9a-f-]{36}):([1-9][0-9]{0,18})$")

DEFAULT_REPLAY_CAPACITY = 512
DEFAULT_SUBSCRIBER_QUEUE_CAPACITY = 64
DEFAULT_MAX_SUBSCRIBERS = 4
DEFAULT_HEARTBEAT_SECONDS = 15.0


class EventStreamUnavailable(RuntimeError):
    """Raised when the database notification listener is not ready."""


class EventStreamCapacityExceeded(RuntimeError):
    """Raised when the configured concurrent subscriber limit is reached."""


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event_id: str
    name: str
    data: dict[str, object]

    def to_sse(self) -> str:
        payload = json.dumps(self.data, separators=(",", ":"), ensure_ascii=True)
        return f"id: {self.event_id}\nevent: {self.name}\ndata: {payload}\n\n"


@dataclass(slots=True, eq=False)
class EventSubscription:
    queue: asyncio.Queue[StreamEvent | None]
    initial_events: tuple[StreamEvent, ...] = ()
    closed_after_initial: bool = False
    closed_after_queue: bool = False
    closed: bool = False


def parse_event_cursor(value: str | None) -> tuple[str, int] | None:
    """Parse a bounded process-epoch cursor; return None for absent/invalid input."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _EVENT_ID_RE.fullmatch(value)
    if match is None:
        return None
    try:
        epoch = str(UUID(match.group(1)))
        sequence = int(match.group(2))
    except (ValueError, OverflowError):
        return None
    if sequence < 1:
        return None
    return epoch, sequence


class ObservabilityEventHub:
    """One-process event ring and bounded fan-out for the single Uvicorn worker."""

    def __init__(
        self,
        *,
        replay_capacity: int = DEFAULT_REPLAY_CAPACITY,
        subscriber_queue_capacity: int = DEFAULT_SUBSCRIBER_QUEUE_CAPACITY,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        random_source=random.random,
    ) -> None:
        if replay_capacity < 1 or subscriber_queue_capacity < 1 or max_subscribers < 1:
            raise ValueError("Event hub capacities must be positive.")
        self.epoch = str(uuid4())
        self._sequence = 0
        self._ring: deque[StreamEvent] = deque(maxlen=replay_capacity)
        self._subscribers: set[EventSubscription] = set()
        self._queue_capacity = subscriber_queue_capacity
        self._max_subscribers = max_subscribers
        self._reconnect_base = reconnect_base_seconds
        self._reconnect_max = reconnect_max_seconds
        self._random = random_source
        self._ready = False
        self._running = False
        self._listener_task: asyncio.Task[None] | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def latest_event_id(self) -> str | None:
        if self._sequence == 0:
            return None
        return f"{self.epoch}:{self._sequence}"

    def _new_event(self, name: str, data: dict[str, object]) -> StreamEvent:
        self._sequence += 1
        event_id = f"{self.epoch}:{self._sequence}"
        event = StreamEvent(event_id=event_id, name=name, data=data)
        self._ring.append(event)
        return event

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _make_reset(self, reason: str) -> StreamEvent:
        return self._new_event(
            "stream.reset",
            {"schema_version": 1, "occurred_at": self._timestamp(), "reason": reason},
        )

    def mark_listener_ready(self) -> None:
        was_ready = self._ready
        self._ready = True
        if not was_ready:
            self._broadcast(self._make_reset("listener_recovered"))

    def mark_listener_unavailable(self) -> None:
        if not self._ready:
            return
        self._ready = False
        self._broadcast(self._make_reset("listener_unavailable"), close_after=True)

    def publish_resources(self, resources: Iterable[str]) -> StreamEvent | None:
        accepted = set(resources) & ALLOWED_RESOURCES
        if not accepted:
            return None
        ordered = [resource for resource in RESOURCE_ORDER if resource in accepted]
        event = self._new_event(
            "resources.invalidated",
            {
                "schema_version": 1,
                "occurred_at": self._timestamp(),
                "resources": ordered,
            },
        )
        self._broadcast(event)
        return event

    def publish_notification(self, payload: str) -> StreamEvent | None:
        """Accept only one of the statically allowlisted resource keys."""
        if payload not in ALLOWED_RESOURCES:
            logger.warning("SSE_NOTIFICATION_REJECTED code=UNKNOWN_RESOURCE")
            return None
        return self.publish_resources((payload,))

    def _broadcast(self, event: StreamEvent, *, close_after: bool = False) -> None:
        for subscription in tuple(self._subscribers):
            if subscription.closed_after_queue:
                continue
            try:
                subscription.queue.put_nowait(event)
                if close_after:
                    subscription.closed_after_queue = True
                    self._subscribers.discard(subscription)
            except asyncio.QueueFull:
                # Replace pending events with a reset marker. Snapshot reconciliation
                # is mandatory after overflow; never buffer without a hard limit.
                while True:
                    try:
                        subscription.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                reset = self._make_reset("subscriber_overflow")
                subscription.queue.put_nowait(reset)
                subscription.closed_after_queue = True
                self._subscribers.discard(subscription)

    def subscribe(self, last_event_id: str | None) -> EventSubscription:
        if not self._ready:
            raise EventStreamUnavailable("Observability event listener is unavailable.")
        if len(self._subscribers) >= self._max_subscribers:
            raise EventStreamCapacityExceeded("Observability stream subscriber capacity is reached.")

        initial: tuple[StreamEvent, ...] = ()
        if last_event_id is not None:
            cursor = parse_event_cursor(last_event_id)
            if cursor is None:
                initial = (self._make_reset("invalid_cursor"),)
            else:
                cursor_epoch, cursor_sequence = cursor
                oldest_sequence = (
                    int(self._ring[0].event_id.rsplit(":", 1)[1])
                    if self._ring else self._sequence + 1
                )
                if cursor_epoch != self.epoch:
                    initial = (self._make_reset("epoch_changed"),)
                elif cursor_sequence > self._sequence or cursor_sequence < oldest_sequence - 1:
                    initial = (self._make_reset("cursor_expired"),)
                else:
                    initial = tuple(
                        event for event in self._ring
                        if int(event.event_id.rsplit(":", 1)[1]) > cursor_sequence
                    )

        subscription = EventSubscription(
            queue=asyncio.Queue(maxsize=self._queue_capacity),
            initial_events=initial,
        )
        if initial and initial[0].name == "stream.reset":
            subscription.closed_after_initial = True
        else:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        self._subscribers.discard(subscription)

    async def next_event(
        self,
        subscription: EventSubscription,
        timeout_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> StreamEvent | None:
        try:
            return await asyncio.wait_for(subscription.queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None

    def start(self, database_url: str) -> None:
        if self._running:
            return
        self._running = True
        dsn = database_url.strip()
        if dsn.startswith("postgresql+psycopg://"):
            dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self._listener_task = asyncio.create_task(self._listener_loop(dsn), name="sireader-observability-listener")

    async def close(self) -> None:
        self._running = False
        task = self._listener_task
        self._listener_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._ready = False
        for subscription in tuple(self._subscribers):
            while True:
                try:
                    subscription.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            subscription.closed = True
            subscription.queue.put_nowait(None)
            self._subscribers.discard(subscription)

    async def _listener_loop(self, database_url: str) -> None:
        failures = 0
        while self._running:
            connection: psycopg.AsyncConnection | None = None
            try:
                connection = await psycopg.AsyncConnection.connect(
                    database_url,
                    autocommit=True,
                    connect_timeout=5,
                    application_name="sireader-observability-listener",
                )
                async with connection.cursor() as cursor:
                    await cursor.execute(f"LISTEN {NOTIFY_CHANNEL}")
                failures = 0
                self.mark_listener_ready()

                # A dedicated autocommit connection keeps LISTEN active without
                # holding a long-running transaction or consuming snapshot pool slots.
                while self._running:
                    resources: set[str] = set()
                    async for notification in connection.notifies(timeout=0.1, stop_after=100):
                        if notification.channel != NOTIFY_CHANNEL:
                            continue
                        if notification.payload in ALLOWED_RESOURCES:
                            resources.add(notification.payload)
                        else:
                            logger.warning("SSE_NOTIFICATION_REJECTED code=UNKNOWN_RESOURCE")
                    if resources:
                        self.publish_resources(resources)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.mark_listener_unavailable()
                failures += 1
                logger.warning("SSE_LISTENER_DISCONNECTED error_type=%s", type(exc).__name__)
                delay = min(self._reconnect_max, self._reconnect_base * (2 ** min(failures - 1, 8)))
                delay *= 0.75 + (self._random() * 0.5)
                await asyncio.sleep(max(0.25, min(self._reconnect_max, delay)))
            finally:
                if connection is not None:
                    try:
                        await connection.close()
                    except Exception:
                        pass
