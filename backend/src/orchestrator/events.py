"""Structured event system.

Storage architecture (bounded by design):

- Lifecycle/structured events (status changes, provider selection, findings,
  gates, checkpoints, …) are persisted to SQLite — these are the authoritative
  mission history and are replayed on WebSocket reconnect.
- High-volume provider output (PROVIDER_OUTPUT) is TRANSIENT: broadcast live
  to subscribers and kept in a small bounded in-memory replay buffer per
  mission, but NEVER written to SQLite. Full raw output already lives in
  per-run log files under <project>/.orchestrator/logs/ — persisting it in
  the database as well would allow a noisy provider to grow SQLite without
  bound.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from .db import Database
from .models import Event, EventType, utcnow
from .security import redact

logger = logging.getLogger(__name__)

Subscriber = Callable[[Event], None]

#: Event types that are never persisted (high-volume streams).
TRANSIENT_TYPES = frozenset({EventType.PROVIDER_OUTPUT})

#: Bounded per-mission replay buffer for transient events (lines).
TRANSIENT_REPLAY_LIMIT = 500


class EventBus:
    def __init__(self, db: Database):
        self._db = db
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._sync_subscribers: list[Subscriber] = []
        self._transient: dict[str, deque[Event]] = defaultdict(lambda: deque(maxlen=TRANSIENT_REPLAY_LIMIT))

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
        if event_type in TRANSIENT_TYPES:
            # Bounded transient channel: memory replay + live broadcast only.
            if mission_id is not None:
                self._transient[mission_id].append(event)
        else:
            self._db.insert(
                "events",
                {
                    "id": event.id,
                    "mission_id": mission_id,
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

    def transient_replay(self, mission_id: str) -> list[Event]:
        """The bounded in-memory tail of transient events for a mission."""
        return list(self._transient.get(mission_id, ()))

    def history(self, mission_id: str, limit: int = 500, include_types: set[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE mission_id = ?"
        params: list[Any] = [mission_id]
        if include_types:
            sql += " AND type IN (" + ",".join("?" for _ in include_types) + ")"
            params.extend(sorted(include_types))
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        rows = self._db.query(sql, tuple(params))
        for row in rows:
            if isinstance(row.get("payload"), str):
                row["payload"] = json.loads(row["payload"])
        return rows
