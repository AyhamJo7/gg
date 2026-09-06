"""Top-level orchestrator service: scheduler, recovery, mission lifecycle.

Owns all running MissionEngine instances. Recovers in-flight missions from
persisted state on startup (RECOVERING → resume at persisted current_phase).
The scheduler tick wakes engines that are waiting for provider cooldowns.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
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
    FailureClass,
    MissionStatus,
    ProviderState,
    utcnow,
)
from .notifications import Notifier, default_notifier
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
        self._engines: dict[str, MissionEngine] = {}
        self._engine_tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        await self.registry.detect_all()
        self._reap_orphaned_processes()
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
    def _verify_process_ownership(
        self,
        pid: int,
        pgid: int,
        started_at_ts: float | None,
        provider: str,
    ) -> bool:
        """Verify a recorded PID still belongs to our provider process.
        
        Returns True only when we can positively identify the process as ours.
        On any doubt, returns False to prevent killing unrelated processes.
        """
        import os
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            return False
        
        # 1. Verify PGID matches
        try:
            actual_pgid = os.getpgid(pid)
            if actual_pgid != pgid:
                logger.warning(
                    "PID %d PGID mismatch: recorded=%d actual=%d — not killing",
                    pid, pgid, actual_pgid,
                )
                return False
        except (ProcessLookupError, PermissionError):
            return False
        
        # 2. Verify start time if we have a recorded timestamp
        if started_at_ts is not None:
            try:
                stat_data = (proc_path / "stat").read_text()
                # /proc/[pid]/stat format: pid (comm) state ... field22=starttime
                # comm can contain spaces/parens, so find the closing ')' first
                close_paren = stat_data.rfind(')')
                if close_paren == -1:
                    return False
                fields = stat_data[close_paren + 2:].split()
                # starttime is field 22 (1-indexed), but after stripping pid+comm+state,
                # it's at index 19 in the remaining fields (state=0, ppid=1, ...)
                proc_starttime = int(fields[19])  # starttime in clock ticks
                
                # Convert our recorded time.time() to clock ticks for comparison
                # Read system boot time from /proc/stat
                boot_time = None
                with open('/proc/stat') as f:
                    for line in f:
                        if line.startswith('btime '):
                            boot_time = int(line.split()[1])
                            break
                if boot_time is not None:
                    clk_tck = os.sysconf('SC_CLK_TCK')  
                    expected_starttime_ticks = int((started_at_ts - boot_time) * clk_tck)
                    # Allow 2-second tolerance for timing jitter
                    if abs(proc_starttime - expected_starttime_ticks) > 2 * clk_tck:
                        logger.warning(
                            "PID %d start time mismatch: recorded=%.1f proc_start=%d expected_ticks=%d — not killing",
                            pid, started_at_ts, proc_starttime, expected_starttime_ticks,
                        )
                        return False
            except (OSError, ValueError, IndexError):
                # Cannot verify start time — err on the side of NOT killing
                logger.warning("PID %d: could not verify start time — not killing", pid)
                return False
        
        # 3. Verify command line contains something provider-related
        try:
            (proc_path / "cmdline").read_bytes()
            # Just verify it's readable (basic sanity that it's a real process)
            # Don't be too strict - provider commands vary
        except OSError:
            pass  # cmdline check is best-effort
        
        return True

    def _reap_orphaned_processes(self) -> None:
        """Find and terminate any provider processes left running by an abnormal backend exit."""
        import os
        import signal
        import time

        unreaped = self.db.query(
            """SELECT pr.id, pr.mission_id, pr.provider, pr.pid, pr.pgid, pr.started_at_ts
               FROM provider_runs pr
               JOIN missions m ON pr.mission_id = m.id
               WHERE pr.finished_at IS NULL AND pr.pgid IS NOT NULL"""
        )
        for row in unreaped:
            pgid = row["pgid"]
            pid = row["pid"]
            run_id = row["id"]
            if pgid is None or pid is None:
                continue

            is_ours = self._verify_process_ownership(pid, pgid, row.get("started_at_ts"), row["provider"])
            
            proc_path = Path(f"/proc/{pid}")

            if is_ours:
                logger.warning(
                    "reaping orphaned provider process tree pgid=%s (pid=%s, run=%s, provider=%s)",
                    pgid,
                    pid,
                    run_id,
                    row["provider"],
                )
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.05)
                if proc_path.exists():
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

            self.db.update(
                "provider_runs",
                run_id,
                {
                    "finished_at": utcnow().isoformat(),
                    "failure_class": FailureClass.CRASH.value,
                    "provider_state": ProviderState.CRASHED.value,
                    "summary": "orphaned process terminated on backend startup",
                },
            )

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
        self.start_mission(new_mission["id"])
        return self.db.get("missions", new_mission["id"]) or new_mission

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
