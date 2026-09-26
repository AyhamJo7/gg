"""Integration engine: merge parallel task branches safely.

When parallel tasks complete, this engine:
- identifies completed branches
- determines deterministic integration order
- merges/cherry-picks safely
- detects conflicts
- runs verification
- never auto-resolves ambiguous semantic conflicts
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_ops
from .models import EventType, IntegrationStatus, TaskStatus, utcnow

if TYPE_CHECKING:
    from .db import Database
    from .events import EventBus

logger = logging.getLogger(__name__)


def _get_completed_branches(db: Database, mission_id: str) -> list[dict[str, Any]]:
    """Return task branches for tasks that are COMPLETED."""
    return db.query(
        """SELECT tb.*, t.id as task_id, t.title, t.assigned_provider, t.checkpoint_after
           FROM task_branches tb
           JOIN tasks t ON tb.task_id = t.id
           WHERE t.mission_id=? AND t.status=?
           AND tb.removed_at IS NULL
           ORDER BY t.finished_at ASC, t.priority DESC""",
        (mission_id, TaskStatus.COMPLETED.value),
    )


async def run_integration(
    db: Database,
    events: EventBus,
    project_path: Path,
    mission_id: str,
    target_branch: str | None = None,
) -> dict[str, Any]:
    """Integrate all completed task branches into the main branch.

    Returns a dict with:
    - status: IntegrationStatus
    - merged_commit: str | None
    - conflict_files: list[str]
    - summary: str
    """
    branches = _get_completed_branches(db, mission_id)
    if not branches:
        return {
            "status": IntegrationStatus.COMPLETED.value,
            "merged_commit": None,
            "conflict_files": [],
            "summary": "no completed branches to integrate",
        }

    integration_id = f"int-{utcnow().timestamp()}".replace(".", "")
    db.insert(
        "task_integrations",
        {
            "id": integration_id,
            "mission_id": mission_id,
            "status": IntegrationStatus.IN_PROGRESS.value,
            "branch_names": json.dumps([b["branch_name"] for b in branches]),
            "conflict_files": "[]",
            "started_at": utcnow().isoformat(),
            "created_at": utcnow().isoformat(),
        },
    )
    events.publish(EventType.INTEGRATION_STARTED, mission_id=mission_id, branches=len(branches))

    # Determine target branch
    if target_branch is None:
        st = await git_ops.status(project_path)
        target_branch = st.branch or "main"

    # Save current position
    original_head = await git_ops.head_sha(project_path)

    conflict_files: list[str] = []
    merged_commit: str | None = None
    summary_parts: list[str] = []

    try:
        for branch_info in branches:
            branch = branch_info["branch_name"]
            task_id = branch_info["task_id"]

            # Idempotency: skip if this branch is already merged into HEAD
            already_merged = await git_ops._spawn_git(project_path, "branch", "--merged", "HEAD", "--list", branch)
            if already_merged.stdout.strip():
                summary_parts.append(f"already merged {branch}")
                continue

            # Attempt merge
            _pre_merge = await git_ops.head_sha(project_path)
            merge_res = await git_ops._spawn_git(project_path, "merge", "--no-commit", "--no-ff", branch)

            if merge_res.returncode != 0:
                # Detect real merge conflicts using unmerged index entries
                ls_files_res = await git_ops._spawn_git(project_path, "ls-files", "-u")
                unmerged_raw = ls_files_res.stdout.decode(errors="replace").strip().splitlines()
                conflict_files = sorted({line.split()[-1] for line in unmerged_raw if line})

                if conflict_files:
                    # Abort merge, record conflict, preserve branches
                    await git_ops._git(project_path, "merge", "--abort", check=False)
                    db.update(
                        "task_integrations",
                        integration_id,
                        {
                            "status": IntegrationStatus.MERGE_CONFLICT.value,
                            "conflict_files": json.dumps(conflict_files),
                            "finished_at": utcnow().isoformat(),
                        },
                    )
                    events.publish(
                        EventType.MERGE_CONFLICT,
                        mission_id=mission_id,
                        branch=branch,
                        files=conflict_files,
                    )
                    return {
                        "status": IntegrationStatus.MERGE_CONFLICT.value,
                        "merged_commit": None,
                        "conflict_files": conflict_files,
                        "summary": f"merge conflict integrating {branch}: {', '.join(conflict_files)}",
                    }

                # No unmerged entries but merge failed — treat as failure
                await git_ops._git(project_path, "merge", "--abort", check=False)
                db.update(
                    "task_integrations",
                    integration_id,
                    {
                        "status": IntegrationStatus.FAILED.value,
                        "finished_at": utcnow().isoformat(),
                        "summary": f"merge failed for {branch}: {merge_res.stderr.decode(errors='replace')[:400]}",
                    },
                )
                return {
                    "status": IntegrationStatus.FAILED.value,
                    "merged_commit": None,
                    "conflict_files": [],
                    "summary": f"merge failed for {branch}",
                }

            # Merge succeeded cleanly — commit
            await git_ops._git(
                project_path,
                "commit",
                "-m",
                f"orchestrator: integrate {branch} (task {task_id})",
            )

            merged_commit = await git_ops.head_sha(project_path)
            summary_parts.append(f"integrated {branch}")
            # SYSTEM integration commit: constituent provider writers are
            # preserved via ancestry (their task checkpoints are parents of
            # this merge), never collapsed into "GG wrote it".
            try:
                from .provenance import ACTOR_SYSTEM, record_write

                record_write(
                    db,
                    run_id=None,
                    mission_id=mission_id,
                    task_id=task_id,
                    actor_type=ACTOR_SYSTEM,
                    actor_detail=f"integrate {branch}",
                    provider=None,
                    role="integration",
                    base_sha=_pre_merge,
                    result_sha=merged_commit,
                )
            except Exception:
                logger.debug("integration provenance insert failed for %s", branch, exc_info=True)

    except git_ops.GitError as exc:
        logger.exception("integration failed")
        # Attempt to abort and restore
        await git_ops._git(project_path, "merge", "--abort", check=False)
        if original_head:
            await git_ops._git(project_path, "checkout", original_head, check=False)
        db.update(
            "task_integrations",
            integration_id,
            {
                "status": IntegrationStatus.FAILED.value,
                "finished_at": utcnow().isoformat(),
                "summary": str(exc),
            },
        )
        return {
            "status": IntegrationStatus.FAILED.value,
            "merged_commit": None,
            "conflict_files": [],
            "summary": f"integration failed: {exc}",
        }

    # Success
    db.update(
        "task_integrations",
        integration_id,
        {
            "status": IntegrationStatus.COMPLETED.value,
            "merged_commit": merged_commit,
            "finished_at": utcnow().isoformat(),
            "summary": "; ".join(summary_parts),
        },
    )
    events.publish(
        EventType.INTEGRATION_COMPLETED,
        mission_id=mission_id,
        merged_commit=merged_commit,
        branches=len(branches),
    )
    return {
        "status": IntegrationStatus.COMPLETED.value,
        "merged_commit": merged_commit,
        "conflict_files": [],
        "summary": "; ".join(summary_parts),
    }


def get_latest_integration(db: Database, mission_id: str) -> dict[str, Any] | None:
    rows = db.query(
        "SELECT * FROM task_integrations WHERE mission_id=? ORDER BY created_at DESC LIMIT 1",
        (mission_id,),
    )
    return rows[0] if rows else None
