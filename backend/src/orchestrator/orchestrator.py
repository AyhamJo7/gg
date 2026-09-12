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
from .models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    EventType,
    MissionStatus,
    SchedulingMode,
    utcnow,
)
from .notifications import Notifier, default_notifier
from .parallel_engine import ParallelMissionEngine
from .project_engine import ProjectCoordinator
from .providers.base import ProviderAdapter
from .providers.registry import ProviderRegistry

TERMINAL_STATUS_VALUES = frozenset(s.value for s in TERMINAL_STATUSES)

logger = logging.getLogger(__name__)


class IllegalMissionTransitionError(ValueError):
    """Raised when attempting an illegal lifecycle transition (e.g. out of terminal states)."""

    pass


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
        self._engines: dict[str, MissionEngine | ParallelMissionEngine] = {}
        self._engine_tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self.coordinator = ProjectCoordinator(self)
        from .repair_worker import RepairWorker

        self.repair_worker = RepairWorker(self)

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        await self.registry.detect_all()
        self._reap_orphaned_processes()
        await self._recover_missions()
        await self.coordinator.recover()
        self.repair_worker.start()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def shutdown(self) -> None:
        self._shutdown.set()
        for engine in self._engines.values():
            engine.request_pause()
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self._engine_tasks:
            await asyncio.gather(*self._engine_tasks.values(), return_exceptions=True)
        await self.repair_worker.stop()

    # -- recovery ---------------------------------------------------------------
    def _verify_process_ownership(
        self,
        pid: int,
        pgid: int,
        started_at_ts: float | None,
        provider: str,
    ) -> bool:
        """Verify a recorded PID still belongs to our provider process.

        Delegates to the shared orphans helper (same strict policy).
        """
        from .orphans import verify_process_ownership

        return verify_process_ownership(pid, pgid, started_at_ts, provider)

    def _reap_orphaned_processes(self) -> None:
        """Reconcile unfinished invocations after backend restart.

        Delegates to the shared invocation boundary so every owner —
        mission runs, parallel tasks, and product-planning invocations
        without a mission — is reconciled with PID-identity safeguards,
        terminal immutability, and lease release. No duplicate writer is
        created and no success is fabricated.
        """
        try:
            from .invocations import InvocationService

            svc = InvocationService(self.db, self.registry, self.config)
            summary = svc.recover()
            logger.warning("invocation recovery reconciled %s", summary)
        except Exception:
            logger.exception("invocation recovery failed")

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
            self.events.publish(
                EventType.MISSION_STATUS_CHANGED, row["id"], status="RECOVERING", reason="backend restart"
            )
            self._launch_engine(row["id"])
            logger.info("recovered mission %s from status %s", row["id"], row["status"])

    # -- scheduler tick ------------------------------------------------------------
    def _workspace_is_busy(self, mission_id: str) -> bool:
        """True if another active mission operates on the same canonical workspace.

        Durable ownership: derived from the missions table (active statuses),
        not in-memory state, so it reconciles correctly across restarts. A
        crashed mission's engine is gone → its row leaves active statuses via
        recovery/pause semantics → the lock releases.
        """
        # Owning statuses: everything except CREATED (never ran), terminal,
        # and WAITING_FOR_WORKSPACE (queued missions own nothing yet — counting
        # them would deadlock two queued missions against each other).
        non_owning = (
            MissionStatus.CREATED.value,
            MissionStatus.WAITING_FOR_WORKSPACE.value,
            *(s.value for s in TERMINAL_STATUSES),
        )
        placeholders = ",".join("?" for _ in non_owning)
        query_sql = (
            "SELECT m2.id FROM missions m1 "  # noqa: S608
            "JOIN missions m2 ON m1.project_id = m2.project_id "
            "WHERE m1.id = ? AND m2.id != ? "
            f"AND m2.status NOT IN ({placeholders})"
        )
        rows = self.db.query(query_sql, (mission_id, mission_id, *non_owning))
        return bool(rows)

    async def _scheduler_loop(self) -> None:
        tick = float(self.config.get("orchestration.scheduler_tick_seconds", 2))
        while not self._shutdown.is_set():
            try:
                self.registry.health()  # expires cooldowns
                for engine in list(self._engines.values()):
                    engine.wake()
                await self.coordinator.advance_all()
                # Bounded autonomous repair: discover runnable repair cycles
                # and spawn one worker task each (non-blocking pump).
                try:
                    await self.repair_worker.pump()
                except Exception:
                    logger.debug("repair pump failed", exc_info=True)
                # re-launch missions that are waiting (provider availability or
                # workspace ownership) when their constraint clears
                waiting = self.db.query(
                    "SELECT id, status FROM missions WHERE status IN (?, ?, ?) ORDER BY created_at ASC, rowid ASC",
                    (
                        MissionStatus.WAITING_FOR_PROVIDER.value,
                        MissionStatus.RATE_LIMITED.value,
                        MissionStatus.WAITING_FOR_WORKSPACE.value,
                    ),
                )
                for row in waiting:
                    if row["id"] in self._engines:
                        continue
                    if row["status"] == MissionStatus.WAITING_FOR_WORKSPACE.value and self._workspace_is_busy(
                        row["id"]
                    ):
                        continue
                    self._launch_engine(row["id"])
                    # launch is synchronous and claims the workspace in the DB,
                    # so the next queued mission in this same tick sees it busy
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
        # Synchronous durable claim: before the engine task exists, the mission
        # row must be in an owning status so concurrent same-workspace launches
        # (same scheduler tick, parallel API calls) see it as busy immediately.
        row = self.db.get("missions", mission_id)
        if row and row["status"] in (
            MissionStatus.CREATED.value,
            MissionStatus.WAITING_FOR_WORKSPACE.value,
        ):
            self.db.update("missions", mission_id, {"status": MissionStatus.RECOVERING.value, "updated_at": utcnow()})

        mode = row.get("scheduling_mode", SchedulingMode.SEQUENTIAL.value) if row else SchedulingMode.SEQUENTIAL.value
        if mode == SchedulingMode.PARALLEL_SAFE.value:
            engine: MissionEngine | ParallelMissionEngine = ParallelMissionEngine(
                mission_id, self.db, self.events, self.registry, self.config
            )
        else:
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
                    "missions",
                    mission_id,
                    {
                        "status": MissionStatus.FAILED.value,
                        "blocking_issue": "engine crash — see backend logs",
                        "updated_at": utcnow(),
                        "finished_at": utcnow(),
                    },
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
        row = self.db.get("missions", mission_id)
        if not row:
            raise KeyError(f"mission {mission_id} not found")
        status = row["status"]
        if status in TERMINAL_STATUS_VALUES:
            raise IllegalMissionTransitionError(
                f"cannot start mission {mission_id}: mission is in terminal state {status}"
            )
        # Same-workspace exclusion: queue instead of running two writers.
        if self._workspace_is_busy(mission_id):
            self.db.update(
                "missions",
                mission_id,
                {"status": MissionStatus.WAITING_FOR_WORKSPACE.value, "updated_at": utcnow()},
            )
            self.events.publish(
                EventType.MISSION_STATUS_CHANGED,
                mission_id,
                status=MissionStatus.WAITING_FOR_WORKSPACE.value,
                reason="another mission owns this workspace",
            )
            return
        self._launch_engine(mission_id)

    def pause_mission(self, mission_id: str) -> None:
        row = self.db.get("missions", mission_id)
        if not row:
            raise KeyError(f"mission {mission_id} not found")
        status = row["status"]
        if status in TERMINAL_STATUS_VALUES:
            raise IllegalMissionTransitionError(
                f"cannot pause mission {mission_id}: mission is in terminal state {status}"
            )
        engine = self._engines.get(mission_id)
        if engine:
            engine.request_pause()
        else:
            self.db.update("missions", mission_id, {"status": MissionStatus.PAUSED.value, "updated_at": utcnow()})
            self.events.publish(EventType.MISSION_PAUSED, mission_id)

    def resume_mission(self, mission_id: str) -> None:
        row = self.db.get("missions", mission_id)
        if not row:
            raise KeyError(f"mission {mission_id} not found")
        status = row["status"]
        if status in TERMINAL_STATUS_VALUES:
            raise IllegalMissionTransitionError(
                f"cannot resume mission {mission_id}: mission is in terminal state {status}"
            )
        if status == MissionStatus.WAITING_FOR_HUMAN.value:
            engine = self._engines.get(mission_id)
            if engine:
                engine.resolve_gate()
                engine.wake()
                return
        if self._workspace_is_busy(mission_id):
            self.db.update(
                "missions",
                mission_id,
                {"status": MissionStatus.WAITING_FOR_WORKSPACE.value, "updated_at": utcnow()},
            )
            self.events.publish(
                EventType.MISSION_STATUS_CHANGED,
                mission_id,
                status=MissionStatus.WAITING_FOR_WORKSPACE.value,
                reason="another mission owns this workspace",
            )
            return
        self._launch_engine(mission_id)

    def cancel_mission(self, mission_id: str) -> None:
        row = self.db.get("missions", mission_id)
        if not row:
            raise KeyError(f"mission {mission_id} not found")
        status = row["status"]
        if status in TERMINAL_STATUS_VALUES:
            # Idempotent no-op on already terminal mission
            return
        engine = self._engines.get(mission_id)
        if engine:
            engine.request_cancel()
        else:
            self.db.update(
                "missions",
                mission_id,
                {"status": MissionStatus.CANCELLED.value, "updated_at": utcnow(), "finished_at": utcnow()},
            )
            self.events.publish(
                EventType.MISSION_STATUS_CHANGED,
                mission_id,
                status=MissionStatus.CANCELLED.value,
                reason="user cancelled",
            )

    def retry_mission(self, mission_id: str) -> dict[str, Any]:
        row = self.db.get("missions", mission_id)
        if not row:
            raise KeyError(f"mission {mission_id} not found")
        # Idempotency: check if a retry already exists for this mission
        existing = self.db.query(
            "SELECT * FROM missions WHERE retry_of_mission_id=? ORDER BY created_at DESC LIMIT 1",
            (mission_id,),
        )
        if existing:
            return existing[0]
        title = row["title"]
        new_title = title if title.startswith("Retry: ") else f"Retry: {title}"
        new_mission = self.create_mission(
            project_id=row["project_id"],
            title=new_title,
            task=row["task"],
            autonomy=row.get("autonomy", "semi-autonomous"),
            profile=row.get("profile", "balanced"),
        )
        # Track lineage
        self.db.update("missions", new_mission["id"], {"retry_of_mission_id": mission_id})
        try:
            self._inherit_retry_findings(mission_id, new_mission["id"])
        except Exception:
            logger.debug("retry finding inheritance failed for %s", mission_id, exc_info=True)
        self.start_mission(new_mission["id"])
        return self.db.get("missions", new_mission["id"]) or new_mission

    def _inherit_retry_findings(self, parent_mission_id: str, retry_mission_id: str) -> int:
        """Seed retry with unresolved ancestor findings (DOG-02).

        Copies open/repair_attempted rows (deduped by fingerprint across the
        full retry chain) as new open/repair_attempted rows in the retry with
        explicit inherited_from_* lineage. Historical rows are never mutated;
        resolved ancestors are never reopened; chains do not multiply because
        fingerprints already present locally are skipped.
        """
        from .review import inherited_open_findings

        # Full ancestor union (not just the immediate parent) so multi-hop
        # chains survive without multiplying: nearest occurrence wins.
        ancestors: list[str] = []
        seen_m: set[str] = {retry_mission_id}
        cur: str | None = parent_mission_id
        for _ in range(20):
            if not cur or cur in seen_m:
                break
            ancestors.append(cur)
            seen_m.add(cur)
            try:
                row = self.db.get("missions", cur)
            except Exception:
                logger.debug("retry chain lookup failed for %s", cur, exc_info=True)
                break
            cur = str(row.get("retry_of_mission_id") or "") if row else ""
            if not cur:
                break
        # Collect nearest-first, deduped by fingerprint.
        wanted: dict[str, dict[str, Any]] = {}
        for anc in ancestors:
            try:
                rows = self.db.query(
                    "SELECT * FROM review_findings WHERE mission_id=? AND status IN ('open','repair_attempted')"
                    " ORDER BY created_at ASC",
                    (anc,),
                )
            except Exception:
                logger.debug("ancestor findings lookup failed for %s", anc, exc_info=True)
                continue
            for r in rows:
                fp = str(r.get("fingerprint") or "")
                if not fp or fp in wanted:
                    continue
                wanted[fp] = dict(r)
        if not wanted:
            return 0
        import uuid as _uuid

        from .models import utcnow as _utcnow

        copied = 0
        for fp, src in wanted.items():
            try:
                exists = self.db.query(
                    "SELECT id FROM review_findings WHERE mission_id=? AND fingerprint=? LIMIT 1",
                    (retry_mission_id, fp),
                )
                if exists:
                    continue
                now = _utcnow().isoformat()
                payload: dict[str, Any] = {
                    "id": f"f-{_uuid.uuid4().hex[:12]}",
                    "mission_id": retry_mission_id,
                    "severity": str(src.get("severity") or "MEDIUM"),
                    "category": str(src.get("category") or "general"),
                    "file": src.get("file"),
                    "description": str(src.get("description") or ""),
                    "recommended_fix": str(src.get("recommended_fix") or ""),
                    "status": str(src.get("status") or "open"),
                    "fingerprint": fp,
                    "created_at": now,
                    "inherited_from_mission_id": str(src.get("mission_id") or parent_mission_id),
                    "inherited_from_finding_id": str(src.get("id") or ""),
                }
                # Preserve origin lineage when present.
                for k in ("origin_review_id", "origin_sha", "verified_by"):
                    try:
                        if src.get(k) is not None:
                            payload[k] = src.get(k)
                    except Exception:
                        logger.debug("origin preserve failed for %s", k, exc_info=True)
                try:
                    self.db.insert("review_findings", payload)
                except Exception:
                    # Pre-migration DBs: retry still inherits without lineage.
                    for k in ("inherited_from_mission_id", "inherited_from_finding_id"):
                        payload.pop(k, None)
                    self.db.insert("review_findings", payload)
                copied += 1
            except Exception:
                logger.debug("retry finding copy failed for %s", fp, exc_info=True)
                continue
        # Ensure query-time fallback also sees them (no-op when copies exist).
        try:
            _ = inherited_open_findings(self.db, retry_mission_id)
        except Exception:
            logger.debug("inherited lookup after copy failed", exc_info=True)
        return copied

    def resolve_gate(self, gate_id: str, resolution: str) -> None:
        gate = self.db.get("human_gates", gate_id)
        if not gate or gate["status"] != "open":
            return
        mission_id = gate["mission_id"]
        mission = self.db.get("missions", mission_id)
        if mission and mission["status"] in TERMINAL_STATUS_VALUES:
            raise IllegalMissionTransitionError(
                f"cannot resolve gate: mission {mission_id} is in terminal state {mission['status']}"
            )
        self.db.update(
            "human_gates",
            gate_id,
            {"status": "resolved", "resolution": resolution, "resolved_at": utcnow()},
        )
        # Cascade to project-gate mirrors so product phases tracking this
        # mission gate unblock together (mirrors are derivative, never
        # authoritative on their own).
        try:
            for mirror in self.db.query(
                "SELECT id, project_id FROM project_gates WHERE mission_gate_id=? AND status='open'", (gate_id,)
            ):
                self.db.update(
                    "project_gates",
                    mirror["id"],
                    {"status": "resolved", "resolution": resolution, "resolved_at": utcnow()},
                )
                self.events.publish(
                    EventType.PRODUCT_GATE_RESOLVED,
                    mission_id,
                    product_project_id=mirror.get("project_id"),
                    gate_id=mirror["id"],
                )
        except Exception:
            logger.debug("project gate mirror cascade failed for %s", gate_id, exc_info=True)
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
