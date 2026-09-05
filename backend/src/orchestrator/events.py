"""Structured event system: persisted to SQLite and broadcast over WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from .db import Database
from .models import Event, EventType, utcnow
from .security import redact

logger = logging.getLogger(__name__)

Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, db: Database):
        self._db = db
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._sync_subscribers: list[Subscriber] = []

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def add_sync_subscriber(self, fn: Subscriber) -> None:
        self._sync_subscribers.append(fn)

    def publish(self, event_type: EventType, mission_id: str | None = None, **payload: Any) -> Event:
        safe_payload = json.loads(redact(json.dumps(payload, default=str)))
        event = Event(mission_id=mission_id, type=event_type, payload=safe_payload, created_at=utcnow())
        self._db.insert(
            "events",
            {
                "id": event.id,
                "mission_id": event.mission_id,
                "type": event.type.value,
                "payload": event.payload,
                "created_at": event.created_at,
            },
        )
        for fn in self._sync_subscribers:
            try:
                fn(event)
            except Exception:  # pragma: no cover - defensive
                logger.exception("sync event subscriber failed")
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass
        return event

    def history(self, mission_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM events WHERE mission_id = ? ORDER BY created_at ASC LIMIT ?",
            (mission_id, limit),
        )
        for row in rows:
            if isinstance(row.get("payload"), str):
                row["payload"] = json.loads(row["payload"])
        return rows
