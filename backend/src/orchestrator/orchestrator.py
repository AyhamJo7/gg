"""Top-level orchestrator service: scheduler, recovery, mission lifecycle.

Owns all running MissionEngine instances. Recovers in-flight missions from
persisted state on startup (RECOVERING → resume at persisted current_phase).
The scheduler tick wakes engines that are waiting for provider cooldowns.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from .config import Config
from .db import Database
from .engine import MissionEngine
from .events import EventBus
from .locks import ResourceLocks
from .models import ACTIVE_STATUSES, TERMINAL_STATUSES, EventType, MissionStatus, utcnow

TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_STATUSES)
from .notifications import Notifier, default_notifier
from .providers.base import ProviderAdapter
from .providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        db: Database,
        config: Config,
        adapters: dict[str, ProviderAdapter],
        notifier: Notifier | None = None,
    ):
        self.db = db
        self.config = config
        self.events = EventBus(db)
        self.registry = ProviderRegistry(db, adapters, config)
        self.locks = ResourceLocks()
        self.notifier = notifier or default_notifier()
        self._engines: dict[str, MissionEngine] = {}
        self._engine_tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        await self.registry.detect_all()
        await self._recover_missions()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def shutdown(self) -> None:
        self._shutdown.set()
        for engine in self._engines.values():
            engine.request_pause()
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self._engine_tasks:
            await asyncio.gather(*self._engine_tasks.values(), return_exceptions=True)

    # -- recovery ---------------------------------------------------------------
    async def _recover_missions(self) -> None:
        rows = self.db.query(
            "SELECT id, status FROM missions WHERE status IN (" + ",".join("?" for _ in ACTIVE_STATUSES) + ")",  # noqa: S608 — placeholders only, values bound below
            tuple(s.value for s in ACTIVE_STATUSES),
        )
        for row in rows:
            # Defense in depth: never recover a mission in a terminal state,
            # even if ACTIVE_STATUSES is edited incorrectly in the future.
            if row["status"] in TERMINAL_STATUS_VALUES:
                logger.warning("skipping terminal mission %s (%s) in recovery", row["id"], row["status"])
                continue
            self.db.update("missions", row["id"], {"status": MissionStatus.RECOVERING.value, "updated_at": utcnow()})
            self.events.publish(EventType.MISSION_STATUS_CHANGED, row["id"], status="RECOVERING",
                reason="backend restart")
            self._launch_engine(row["id"])
            logger.info("recovered mission %s from status %s", row["id"], row["status"])

    # -- scheduler tick ------------------------------------------------------------
    async def _scheduler_loop(self) -> None:
        tick = float(self.config.get("orchestration.scheduler_tick_seconds", 2))
        while not self._shutdown.is_set():
            try:
                self.registry.health()  # expires cooldowns
                for engine in list(self._engines.values()):
                    engine.wake()
                # re-launch missions that are waiting and now have an eligible provider
                waiting = self.db.query(
                    "SELECT id, current_phase FROM missions WHERE status IN (?, ?)",
                    (MissionStatus.WAITING_FOR_PROVIDER.value, MissionStatus.RATE_LIMITED.value),
                )
                for row in waiting:
                    if row["id"] not in self._engines:
                        self._launch_engine(row["id"])
            except Exception:  # pragma: no cover - defensive
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=tick)
            except TimeoutError:
                continue

    # -- engine management ----------------------------------------------------------
    def _launch_engine(self, mission_id: str) -> None:
        if mission_id in self._engines:
            self._engines[mission_id].wake()
            return
        engine = MissionEngine(mission_id, self.db, self.events, self.registry, self.config, self.locks)
        self._engines[mission_id] = engine

        async def runner() -> None:
            try:
                await engine.run()
            except Exception:
                logger.exception("mission %s engine crashed", mission_id)
                # Durable terminal state FIRST, best-effort event SECOND.
                # A failing event bus must never kill the runner or mask the
                # persisted FAILED state (recovery reads the DB, not events).
                self.db.update(
                    "missions", mission_id,
                    {"status": MissionStatus.FAILED.value, "blocking_issue": "engine crash — see backend logs",
                        "updated_at": utcnow(), "finished_at": utcnow()},
                )
                try:
                    self.events.publish(EventType.MISSION_FAILED, mission_id, reason="engine crash")
                except Exception:  # pragma: no cover - defensive
                    logger.exception("failed to publish MISSION_FAILED for %s", mission_id)
            finally:
                self._engines.pop(mission_id, None)
                self._engine_tasks.pop(mission_id, None)

        self._engine_tasks[mission_id] = asyncio.create_task(runner())

    # -- mission API operations --------------------------------------------------------
    def create_mission(self, project_id: str, title: str, task: str, autonomy: str, profile: str) -> dict[str, Any]:
        mission_id = uuid.uuid4().hex[:16]
        self.db.insert(
            "missions",
            {
                "id": mission_id,
                "project_id": project_id,
                "title": title,
                "task": task,
                "status": MissionStatus.CREATED.value,
                "autonomy": autonomy,
                "profile": profile,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        self.events.publish(EventType.MISSION_CREATED, mission_id, title=title, project_id=project_id)
        return self.db.get("missions", mission_id) or {}

    def start_mission(self, mission_id: str) -> None:
        self._launch_engine(mission_id)

    def pause_mission(self, mission_id: str) -> None:
        engine = self._engines.get(mission_id)
        if engine:
            engine.request_pause()
        else:
            self.db.update("missions", mission_id, {"status": MissionStatus.PAUSED.value, "updated_at": utcnow()})

    def resume_mission(self, mission_id: str) -> None:
        row = self.db.get("missions", mission_id)
        if not row:
            return
        if row["status"] == MissionStatus.WAITING_FOR_HUMAN.value:
            engine = self._engines.get(mission_id)
            if engine:
                engine.resolve_gate()
                return
        self._launch_engine(mission_id)

    def cancel_mission(self, mission_id: str) -> None:
        engine = self._engines.get(mission_id)
        if engine:
            engine.request_cancel()
        else:
            self.db.update(
                "missions", mission_id,
                {"status": MissionStatus.CANCELLED.value, "updated_at": utcnow(), "finished_at": utcnow()},
            )

    def resolve_gate(self, gate_id: str, resolution: str) -> None:
        gate = self.db.get("human_gates", gate_id)
        if not gate or gate["status"] != "open":
            return
        self.db.update(
            "human_gates", gate_id,
            {"status": "resolved", "resolution": resolution, "resolved_at": utcnow()},
        )
        mission_id = gate["mission_id"]
        self.events.publish(EventType.HUMAN_GATE_RESOLVED, mission_id, gate_id=gate_id, resolution=resolution)
        if resolution.lower() in ("cancel", "cancel mission"):
            self.cancel_mission(mission_id)
            return
        if resolution.lower().startswith("skip"):
            # push the blocking provider into a short cooldown so selection skips it
            from datetime import UTC, datetime, timedelta

            from .models import ProviderState

            mission = self.db.get("missions", mission_id)
            if mission and mission.get("current_provider"):
                until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
                self.db.execute(
                    "UPDATE providers SET state=?, cooldown_until=? WHERE name=?",
                    (ProviderState.RATE_LIMITED.value, until, mission["current_provider"]),
                )
        self.resume_mission(mission_id)

    # -- analytics -----------------------------------------------------------------------
    def analytics(self) -> dict[str, Any]:
        missions = self.db.query("SELECT status, COUNT(*) AS n FROM missions GROUP BY status")
        runs = self.db.query(
            """SELECT provider, COUNT(*) AS runs,
                      SUM(CASE WHEN failure_class='NONE' THEN 1 ELSE 0 END) AS successes,
                      SUM(CASE WHEN failure_class IN ('RATE_LIMIT','QUOTA_EXHAUSTED') THEN 1 ELSE 0 END) AS rate_limits,
                      AVG(CASE WHEN finished_at IS NOT NULL THEN
                          (julianday(finished_at) - julianday(started_at)) * 86400.0 END) AS avg_seconds
               FROM provider_runs GROUP BY provider"""
        )
        findings = self.db.query("SELECT severity, COUNT(*) AS n FROM review_findings GROUP BY severity")
        return {
            "missions_by_status": {r["status"]: r["n"] for r in missions},
            "provider_stats": runs,
            "findings_by_severity": {r["severity"]: r["n"] for r in findings},
        }
