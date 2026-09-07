"""Git worktree isolation for parallel task execution.

Each parallel task operates in its own isolated worktree on a namespaced branch.
This prevents concurrent writers from colliding and provides full Git
auditability per task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from . import git_ops
from .models import EventType, TaskBranch, utcnow

if TYPE_CHECKING:
    from .db import Database
    from .events import EventBus

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "gg"


def _task_branch_name(mission_id: str, task_id: str) -> str:
    return f"{BRANCH_PREFIX}/{mission_id[:12]}/{task_id[:16]}"


def _worktree_path(project_path: Path, mission_id: str, task_id: str) -> Path:
    return project_path / ".orchestrator" / "worktrees" / mission_id[:12] / task_id[:16]


async def create_task_worktree(
    db: Database,
    events: EventBus,
    project_path: Path,
    mission_id: str,
    task_id: str,
    base_commit: str | None = None,
) -> TaskBranch:
    """Create a git worktree + branch for a task.

    Never overwrites existing user branches.  Never force-checkout.
    Reuses an existing active worktree if one already exists for the task.
    """
    # Reuse existing active branch record
    existing_rows = db.query(
        "SELECT * FROM task_branches WHERE task_id=? AND removed_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    )
    if existing_rows:
        row = existing_rows[0]
        if await asyncio.to_thread(os.path.exists, row["worktree_path"]):
            return TaskBranch(
                id=row["id"],
                task_id=row["task_id"],
                branch_name=row["branch_name"],
                base_commit=row["base_commit"],
                worktree_path=row["worktree_path"],
                created_at=row["created_at"],
            )

    branch = _task_branch_name(mission_id, task_id)
    wt_path = _worktree_path(project_path, mission_id, task_id)

    # Ensure base commit
    if base_commit is None:
        base_commit = await git_ops.head_sha(project_path)
    if not base_commit:
        raise git_ops.GitError("cannot create worktree: no base commit")

    # Check if branch already exists (user or previous task)
    existing = await git_ops._spawn_git(project_path, "branch", "--list", branch)
    if existing.stdout.strip():
        # Append a short suffix to avoid collision
        import uuid

        branch = f"{branch}-{uuid.uuid4().hex[:4]}"
        wt_path = wt_path.parent / f"{wt_path.name}-{uuid.uuid4().hex[:4]}"

    # Ensure parent directory exists
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # Create worktree
    await git_ops._git(
        project_path,
        "worktree",
        "add",
        "-b",
        branch,
        str(wt_path),
        base_commit,
    )

    # Configure worktree git user
    await git_ops._git(wt_path, "config", "user.email", "orchestrator@local")
    await git_ops._git(wt_path, "config", "user.name", "GG Orchestrator")

    branch_record = TaskBranch(
        task_id=task_id,
        branch_name=branch,
        base_commit=base_commit,
        worktree_path=str(wt_path),
    )
    db.insert(
        "task_branches",
        {
            "id": branch_record.id,
            "task_id": branch_record.task_id,
            "branch_name": branch_record.branch_name,
            "base_commit": branch_record.base_commit,
            "worktree_path": branch_record.worktree_path,
            "created_at": branch_record.created_at,
        },
    )
    events.publish(
        EventType.WORKTREE_CREATED,
        task_id=task_id,
        branch=branch,
        worktree_path=str(wt_path),
    )
    logger.info("created worktree %s for task %s on branch %s", wt_path, task_id, branch)
    return branch_record


async def remove_task_worktree(
    db: Database,
    events: EventBus,
    project_path: Path,
    task_id: str,
) -> None:
    """Remove a task worktree and clean up the branch.

    Only removes artifacts proven to belong to GG (namespaced branches).
    """
    row = db.query("SELECT * FROM task_branches WHERE task_id=? AND removed_at IS NULL", (task_id,))
    if not row:
        return
    record = row[0]
    wt_path = Path(record["worktree_path"])
    branch = record["branch_name"]

    if not branch.startswith(f"{BRANCH_PREFIX}/"):
        logger.warning("refusing to remove non-GG branch %s", branch)
        return

    # Remove worktree
    try:
        await git_ops._git(project_path, "worktree", "remove", "-f", str(wt_path), check=False)
    except git_ops.GitError as exc:
        logger.warning("worktree remove failed for %s: %s", wt_path, exc)

    # Delete branch from main repo
    try:
        await git_ops._git(project_path, "branch", "-D", branch, check=False)
    except git_ops.GitError as exc:
        logger.warning("branch delete failed for %s: %s", branch, exc)

    db.update("task_branches", record["id"], {"removed_at": utcnow().isoformat()})
    events.publish(EventType.WORKTREE_REMOVED, task_id=task_id, branch=branch)
    logger.info("removed worktree %s for task %s", wt_path, task_id)


async def get_task_worktree_path(db: Database, task_id: str) -> Path | None:
    rows = db.query("SELECT worktree_path FROM task_branches WHERE task_id=? AND removed_at IS NULL", (task_id,))
    return Path(rows[0]["worktree_path"]) if rows else None


async def checkpoint_in_worktree(
    project_path: Path,
    task_id: str,
    message: str,
    db: Database,
    max_file_mb: int = 5,
) -> str | None:
    """Create a checkpoint inside a task worktree."""
    wt_path = await get_task_worktree_path(db, task_id)
    if not wt_path:
        logger.warning("no worktree for task %s; falling back to main repo", task_id)
        return await git_ops.checkpoint(project_path, message, max_file_mb=max_file_mb)
    return await git_ops.checkpoint(wt_path, message, max_file_mb=max_file_mb)


async def list_gg_worktrees(project_path: Path) -> list[dict]:
    """List all worktrees managed by GG (namespaced branches)."""
    res = await git_ops._spawn_git(project_path, "worktree", "list", "--porcelain")
    if res.returncode != 0:
        return []
    worktrees: list[dict] = []
    current: dict = {}
    for line in res.stdout.decode(errors="replace").splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line[len("worktree ") :].strip()}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].strip().replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :].strip()
    if current:
        worktrees.append(current)
    return [wt for wt in worktrees if wt.get("branch", "").startswith(f"{BRANCH_PREFIX}/")]
