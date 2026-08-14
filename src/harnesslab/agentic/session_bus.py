from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class SessionEvent(BaseModel):
    event_id: str
    sequence: int = Field(ge=1)
    session_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionEventSubscription:
    def __init__(
        self,
        bus: SessionEventBus,
        session_id: str,
        queue: asyncio.Queue[SessionEvent],
    ) -> None:
        self._bus = bus
        self.session_id = session_id
        self._queue = queue
        self._closed = False

    async def get(self) -> SessionEvent:
        if self._closed:
            raise RuntimeError("subscription is closed")
        return await self._queue.get()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._remove_subscription(self.session_id, self._queue)


class SessionEventBus:
    """Bounded ordered event history with scoped live subscriptions."""

    def __init__(self, *, history_limit: int = 256, subscriber_queue_limit: int = 64) -> None:
        if history_limit < 1 or subscriber_queue_limit < 1:
            raise ValueError("history_limit and subscriber_queue_limit must be positive")
        self.history_limit = history_limit
        self.subscriber_queue_limit = subscriber_queue_limit
        self._sequence = 0
        self._history: dict[str, deque[SessionEvent]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._subscriptions: dict[str, list[asyncio.Queue[SessionEvent]]] = defaultdict(list)

    async def publish(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> SessionEvent:
        self._sequence += 1
        event = SessionEvent(
            event_id=f"evt_{self._sequence:012d}",
            sequence=self._sequence,
            session_id=session_id,
            event_type=event_type,
            payload=payload or {},
        )
        self._history[session_id].append(event)
        for queue in self._subscriptions[session_id][:]:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        return event

    def history(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        return [event for event in self._history[session_id] if event.sequence > after_sequence]

    def subscribe(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> SessionEventSubscription:
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=self.subscriber_queue_limit)
        replay = self.history(session_id, after_sequence=after_sequence)
        for event in replay[-self.subscriber_queue_limit :]:
            queue.put_nowait(event)
        self._subscriptions[session_id].append(queue)
        return SessionEventSubscription(self, session_id, queue)

    def _remove_subscription(self, session_id: str, queue: asyncio.Queue[SessionEvent]) -> None:
        subscriptions = self._subscriptions[session_id]
        if queue in subscriptions:
            subscriptions.remove(queue)
