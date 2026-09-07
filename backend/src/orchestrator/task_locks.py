"""Persistent resource lock system.

Two agents must not edit overlapping resources concurrently.
Locks are stored in SQLite so restart recovery can reconstruct them.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .models import EventType, LockType, utcnow

if TYPE_CHECKING:
    from .db import Database
    from .events import EventBus

logger = logging.getLogger(__name__)


def acquire_locks(
    db: Database,
    events: EventBus,
    task_id: str,
    lock_types: list[tuple[LockType, str]],
) -> tuple[bool, str]:
    """Acquire all requested locks atomically for a task.

    Returns (success, reason).  If any lock is already held, none are acquired.
    """
    if not lock_types:
        return True, ""

    with db.cursor() as cur:
        # Check for conflicts
        for lt, key in lock_types:
            # Exact resource key match
            conflict = cur.execute(
                """SELECT tl.task_id, t.status FROM task_locks tl
                   JOIN tasks t ON tl.task_id = t.id
                   WHERE tl.resource_key=? AND tl.released_at IS NULL
                   AND tl.task_id != ?
                   AND t.status IN ('CLAIMED','RUNNING','WAITING_FOR_PROVIDER','WAITING_FOR_HUMAN')""",
                (key, task_id),
            ).fetchone()
            if conflict:
                reason = f"{lt.value} lock on '{key}' held by task {conflict['task_id']}"
                events.publish(EventType.LOCK_CONFLICT, task_id=task_id, reason=reason)
                return False, reason

            # GIT locks are global per repository path
            if lt == LockType.GIT:
                git_conflict = cur.execute(
                    """SELECT tl.task_id FROM task_locks tl
                       JOIN tasks t ON tl.task_id = t.id
                       WHERE tl.lock_type='GIT' AND tl.released_at IS NULL
                       AND tl.task_id != ?
                       AND t.status IN ('CLAIMED','RUNNING','WAITING_FOR_PROVIDER')""",
                    (task_id,),
                ).fetchone()
                if git_conflict:
                    reason = f"GIT lock held by task {git_conflict['task_id']}"
                    return False, reason

        # Acquire all
        for lt, key in lock_types:
            lid = f"lck-{utcnow().timestamp()}".replace(".", "")
            cur.execute(
                "INSERT INTO task_locks(id, task_id, lock_type, resource_key, acquired_at) VALUES (?,?,?,?,?)",
                (lid, task_id, lt.value, key, utcnow().isoformat()),
            )
            events.publish(EventType.LOCK_ACQUIRED, task_id=task_id, lock_type=lt.value, resource_key=key)

    return True, ""


def release_locks_for_task(
    db: Database,
    events: EventBus,
    task_id: str,
) -> None:
    """Release all locks held by a task."""
    rows = db.query(
        "SELECT id, lock_type, resource_key FROM task_locks WHERE task_id=? AND released_at IS NULL",
        (task_id,),
    )
    if not rows:
        return
    for row in rows:
        db.update("task_locks", row["id"], {"released_at": utcnow().isoformat()})
        events.publish(
            EventType.LOCK_RELEASED,
            task_id=task_id,
            lock_type=row["lock_type"],
            resource_key=row["resource_key"],
        )


def locks_for_task(db: Database, task_id: str) -> list[dict]:
    return db.query(
        "SELECT * FROM task_locks WHERE task_id=? AND released_at IS NULL ORDER BY acquired_at",
        (task_id,),
    )


def compute_task_locks(task_row: dict) -> list[tuple[LockType, str]]:
    """Derive the lock set a task needs from its workspace_scope."""
    scope_raw = task_row.get("workspace_scope") or "[]"
    if isinstance(scope_raw, str):
        scopes = json.loads(scope_raw)
    else:
        scopes = list(scope_raw)

    locks: list[tuple[LockType, str]] = []
    for scope in scopes:
        if scope in (".", "**", ""):
            locks.append((LockType.WORKSPACE_EXCLUSIVE, "workspace"))
        else:
            locks.append((LockType.PATH_PREFIX, scope.strip().replace("\\", "/")))
    return locks
