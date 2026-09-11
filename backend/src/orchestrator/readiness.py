"""Task readiness engine.

Idempotent, deterministic calculation of which tasks in a mission DAG are
ready to execute.  Readiness is computed from durable database state — never
from in-memory assumptions — so restart recovery reconstructs the exact same
READY set.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .models import MissionStatus, TaskStatus

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


def _task_rows(db: Database, mission_id: str) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM tasks WHERE mission_id=? ORDER BY priority DESC, created_at ASC", (mission_id,))


def _deps_for(db: Database, task_id: str) -> list[str]:
    rows = db.query("SELECT from_task_id FROM task_dependencies WHERE to_task_id=?", (task_id,))
    return [r["from_task_id"] for r in rows]


def _active_locks(db: Database, resource_key: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM task_locks WHERE resource_key=? AND released_at IS NULL",
        (resource_key,),
    )


def _open_gates_for_mission(db: Database, mission_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM human_gates WHERE mission_id=? AND status='open'",
        (mission_id,),
    )


def _scopes_conflict(a: str, b: str) -> bool:
    """Deterministic scope intersection.

    Supports exact files, directory prefixes, and glob-like whole-workspace.
    Conservative: if uncertain, returns True (serialize).
    """
    a = a.strip().replace("\\", "/")
    b = b.strip().replace("\\", "/")

    # Whole workspace overlaps everything
    if a == "**" or a == "." or b == "**" or b == ".":
        return True

    # Normalize: remove leading ./ and trailing /**
    def norm(s: str) -> str:
        while s.startswith("./"):
            s = s[2:]
        if s.endswith("/**"):
            s = s[:-3]
        return s.rstrip("/")

    a_n = norm(a)
    b_n = norm(b)

    if not a_n or not b_n:
        return True

    # Exact match
    if a_n == b_n:
        return True

    # Directory prefix: one is a parent of the other
    if a_n.startswith(b_n + "/") or b_n.startswith(a_n + "/"):
        return True

    # Wildcard suffix match (e.g. backend/** vs backend/api/**)
    if "*" in a or "*" in b:
        # Conservative: if prefixes share a common directory root, conflict
        a_parts = a_n.split("/")
        b_parts = b_n.split("/")
        min_len = min(len(a_parts), len(b_parts))
        if min_len > 0 and a_parts[0] == b_parts[0]:
            # If either ends with ** or *, treat as prefix
            if a.endswith("*") or b.endswith("*"):
                return True

    return False


def _task_has_resource_conflict(db: Database, task_row: dict[str, Any], ready_task_ids: set[str]) -> tuple[bool, str]:
    """Return (conflict, reason) for the task against currently running/claimed tasks."""
    scope_raw = task_row.get("workspace_scope") or "[]"
    if isinstance(scope_raw, str):
        scopes = json.loads(scope_raw)
    else:
        scopes = list(scope_raw)

    if not scopes:
        # No explicit scope → whole workspace
        scopes = ["."]

    # Check against active locks
    my_task_id = task_row["id"]
    active = db.query(
        """SELECT tl.*, t.id as task_id, t.status as task_status
           FROM task_locks tl
           JOIN tasks t ON tl.task_id = t.id
           WHERE tl.released_at IS NULL
           AND tl.task_id != ?
           AND t.status IN ('CLAIMED','RUNNING','WAITING_FOR_PROVIDER')""",
        (my_task_id,),
    )

    for lock in active:
        for my_scope in scopes:
            if _scopes_conflict(my_scope, lock["resource_key"]):
                return (
                    True,
                    f"scope '{my_scope}' conflicts with lock '{lock['resource_key']}' held by task {lock['task_id']}",
                )

    # Also check against other ready tasks that haven't acquired locks yet
    # (prevents two READY tasks with overlapping scopes from both launching)
    for other_id in ready_task_ids:
        if other_id == my_task_id:
            continue
        other = db.get("tasks", other_id)
        if not other:
            continue
        other_scopes_raw = other.get("workspace_scope") or "[]"
        if isinstance(other_scopes_raw, str):
            other_scopes = json.loads(other_scopes_raw)
        else:
            other_scopes = list(other_scopes_raw)
        if not other_scopes:
            other_scopes = ["."]
        for my_scope in scopes:
            for other_scope in other_scopes:
                if _scopes_conflict(my_scope, other_scope):
                    return True, f"scope '{my_scope}' conflicts with task {other_id} scope '{other_scope}'"

    return False, ""


def detect_permanent_blockage(db: Database, mission_id: str) -> list[str]:
    """Return task IDs permanently blocked by failed/cancelled/unverified dependencies."""
    rows = _task_rows(db, mission_id)
    status_map: dict[str, str] = {r["id"]: r["status"] for r in rows}
    permanently_blocked: list[str] = []
    for task in rows:
        tid = task["id"]
        status = status_map.get(tid, TaskStatus.PENDING.value)
        terminal = (
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.UNVERIFIED.value,
        )
        if status in terminal:
            continue
        deps = _deps_for(db, tid)
        for dep in deps:
            dep_status = status_map.get(dep, TaskStatus.PENDING.value)
            if dep_status in (
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
                TaskStatus.UNVERIFIED.value,
            ):
                permanently_blocked.append(tid)
                break
    return permanently_blocked


def compute_ready_tasks(db: Database, mission_id: str) -> list[dict[str, Any]]:
    """Idempotent computation of tasks that are READY.

    A task is READY when:
    - all dependencies are COMPLETED
    - no unresolved Human Gate
    - required resources available (no workspace conflict)
    - mission not paused/cancelled/failed
    """
    mission = db.get("missions", mission_id)
    if not mission:
        logger.warning("mission %s not found during readiness check", mission_id)
        return []

    if mission["status"] in (MissionStatus.PAUSED.value, MissionStatus.CANCELLED.value, MissionStatus.FAILED.value):
        return []

    rows = _task_rows(db, mission_id)
    if not rows:
        return []

    # Build status map
    status_map: dict[str, str] = {r["id"]: r["status"] for r in rows}

    # Open gates block everything in SAFE mode; in other modes they only block
    # the specific task if the gate is attached to it.  For simplicity in v2a,
    # any open gate blocks new task launches.
    open_gates = _open_gates_for_mission(db, mission_id)
    if open_gates:
        return []

    ready: list[dict[str, Any]] = []
    ready_ids: set[str] = set()

    for task in rows:
        tid = task["id"]
        status = status_map.get(tid, TaskStatus.PENDING.value)

        # WAITING_FOR_PROVIDER is eligible for re-evaluation
        if status not in (
            TaskStatus.PENDING.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.WAITING_FOR_PROVIDER.value,
        ):
            continue

        deps = _deps_for(db, tid)
        blocked = False
        block_reason = ""

        for dep in deps:
            dep_status = status_map.get(dep, TaskStatus.PENDING.value)
            if dep_status != TaskStatus.COMPLETED.value:
                blocked = True
                block_reason = f"waiting for dependency {dep} ({dep_status})"
                break

        if not blocked:
            conflict, conflict_reason = _task_has_resource_conflict(db, task, ready_ids)
            if conflict:
                blocked = True
                block_reason = conflict_reason

        if blocked:
            if status != TaskStatus.BLOCKED.value:
                db.update("tasks", tid, {"status": TaskStatus.BLOCKED.value, "blocking_issue": block_reason})
            continue

        ready.append(task)
        ready_ids.add(tid)

    return ready
