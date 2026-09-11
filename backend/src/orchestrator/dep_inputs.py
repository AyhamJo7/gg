"""Deterministic DAG dependency execution artifacts (Increment 3B).

Every executable task runs against one pinned ``input_sha``:

- root task: the mission DAG base SHA,
- single dependency: that dependency's committed result SHA,
- multiple dependencies: a deterministic SYSTEM merge artifact containing
  every required dependency result (task-ID order; fast-forward when one
  result already covers the rest).

Scheduling order (readiness) and context handoffs (Increment 2) are
necessary but not sufficient: this module proves ARTIFACT inclusion through
Git ancestry before any provider capacity is touched. Merge conflicts,
missing results, and bad SHAs block the task with zero provider calls.

No LLM calls. No autonomous repair. Local git plumbing only.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import utcnow
from .provenance import ACTOR_SYSTEM, normalize_sha, record_write, repo_identity

logger = logging.getLogger(__name__)


class DependencyInputError(RuntimeError):
    """Task cannot start: dependency artifact unavailable or invalid.

    Carries a stable ``code`` for status mapping: MISSING_RESULT,
    BAD_SHA, WRONG_REPO, UNPROVENANCED, CONFLICT, INTEGRATION_INVALID,
    NO_BASE. No provider capacity may be consumed on these paths.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class DependencyResult:
    task_id: str
    result_sha: str


@dataclass
class PreparedInput:
    input_sha: str
    input_tree_sha: str | None
    integration_sha: str | None
    dependencies: list[DependencyResult] = field(default_factory=list)
    reused: bool = False


def dependency_ids(db: Any, task_id: str) -> list[str]:
    """Direct hard dependencies in deterministic task-ID order (D-07)."""
    try:
        rows = db.query(
            "SELECT from_task_id FROM task_dependencies WHERE to_task_id=? ORDER BY from_task_id ASC",
            (task_id,),
        )
    except Exception:
        return []
    seen: list[str] = []
    for r in rows:
        dep = str(r.get("from_task_id") or "")
        if dep and dep not in seen:
            seen.append(dep)
    return seen


def effective_task_result(db: Any, task: dict[str, Any]) -> str | None:
    """Best-known committed result for a task: explicit result, checkpoint,
    then latest provider run linkage. None when unprovable (D-04)."""
    for key in ("result_sha", "checkpoint_after"):
        sha = normalize_sha(task.get(key))
        if sha:
            return sha
    try:
        runs = db.query(
            "SELECT git_commit_after FROM provider_runs WHERE task_id=? AND failure_class='NONE'"
            " ORDER BY started_at DESC LIMIT 1",
            (task.get("id"),),
        )
    except Exception:
        return None
    if runs:
        return normalize_sha(runs[0].get("git_commit_after"))
    return None


async def dependency_results(
    db: Any, repo: Path, mission_id: str, task_id: str
) -> tuple[list[DependencyResult], list[str]]:
    """Resolve + validate every hard dependency result (D-03/D-04/D-20/D-21).

    Each dependency must be COMPLETED with a real commit that exists in THIS
    repo and carries a matching-repo write provenance row (fail closed on
    UNKNOWN_EXTERNAL per Increment 3 §45). Returns (results, problems).
    """
    from . import git_ops

    results: list[DependencyResult] = []
    problems: list[str] = []
    expected_key = await repo_identity(repo)
    for dep_id in dependency_ids(db, task_id):
        dep = db.get("tasks", dep_id)
        if dep is None or dep.get("mission_id") != mission_id:
            problems.append(f"unknown dependency {dep_id}")
            continue
        if dep.get("status") != "COMPLETED":
            problems.append(f"dependency {dep_id} is {dep.get('status')}, not COMPLETED")
            continue
        result = effective_task_result(db, dep)
        if result is None:
            problems.append(f"dependency {dep_id} has no committed result SHA")
            continue
        if not await git_ops.commit_exists(repo, result):
            problems.append(f"dependency {dep_id} result {result[:8]} does not exist in this repository")
            continue
        rows = (
            db.query(
                "SELECT id FROM write_provenance WHERE result_sha=? AND repo_key=? LIMIT 1", (result, expected_key)
            )
            if expected_key
            else []
        )
        if not rows:
            problems.append(f"dependency {dep_id} result {result[:8]} has no repository provenance")
            continue
        results.append(DependencyResult(task_id=dep_id, result_sha=result))
    return results, problems


async def prepare_task_input(
    db: Any,
    repo: Path,
    mission_id: str,
    task_id: str,
    dag_base_sha: str | None,
    attempt_number: int = 0,
) -> PreparedInput:
    """Build (or reuse) the pinned input artifact for one task attempt.

    Must run BEFORE provider reservation/lease: all failure modes raise
    DependencyInputError with zero provider side effects (D-09). Restart-safe
    and idempotent: a stored row matching the same dependency SHAs whose
    input commit still exists with valid ancestry is reused (D-24/D-40).
    """
    from . import git_ops

    task = db.get("tasks", task_id)
    if task is None or task.get("mission_id") != mission_id:
        raise DependencyInputError("UNKNOWN_TASK", f"task {task_id} not found")
    results, problems = await dependency_results(db, repo, mission_id, task_id)
    if problems:
        raise DependencyInputError("MISSING_RESULT", "; ".join(problems)[:800])
    dep_set = [{"task_id": r.task_id, "result_sha": r.result_sha} for r in results]
    dep_set_json = _stable_json(dep_set)

    reused = _find_reusable_input(db, repo, task_id, dep_set_json)
    if reused is not None:
        return reused

    base = normalize_sha(dag_base_sha)
    if not results:
        if base is None or not await git_ops.commit_exists(repo, base):
            raise DependencyInputError("NO_BASE", "mission DAG base SHA unavailable")
        tree = await git_ops.tree_sha(repo, base)
        prepared = PreparedInput(input_sha=base, input_tree_sha=tree, integration_sha=None, dependencies=[])
        _store_input_row(db, mission_id, task_id, attempt_number, dep_set_json, prepared, "READY", "")
        return prepared
    if len(results) == 1:
        only = results[0].result_sha
        if base and not await git_ops.is_ancestor(repo, base, only):
            raise DependencyInputError(
                "INTEGRATION_INVALID", f"dependency result {only[:8]} does not descend from DAG base"
            )
        tree = await git_ops.tree_sha(repo, only)
        prepared = PreparedInput(input_sha=only, input_tree_sha=tree, integration_sha=None, dependencies=results)
        _store_input_row(db, mission_id, task_id, attempt_number, dep_set_json, prepared, "READY", "")
        return prepared

    # Multi-dependency: fast-forward when one result already covers the rest.
    ordered = sorted(results, key=lambda r: r.task_id)
    for candidate in ordered:
        covers_all = True
        for other in ordered:
            if other.result_sha == candidate.result_sha:
                continue
            if not await git_ops.is_ancestor(repo, other.result_sha, candidate.result_sha):
                covers_all = False
                break
        if covers_all:
            tree = await git_ops.tree_sha(repo, candidate.result_sha)
            prepared = PreparedInput(
                input_sha=candidate.result_sha, input_tree_sha=tree, integration_sha=None, dependencies=ordered
            )
            _store_input_row(db, mission_id, task_id, attempt_number, dep_set_json, prepared, "READY", "")
            return prepared

    # Deterministic SYSTEM merge in task-ID order (D-07): same dependency set
    # always merges in the same order; same content yields the same tree.
    current = ordered[0].result_sha
    merged_any = False
    for nxt in ordered[1:]:
        if await git_ops.is_ancestor(repo, nxt.result_sha, current):
            continue
        if await git_ops.is_ancestor(repo, current, nxt.result_sha):
            current = nxt.result_sha
            merged_any = True
            continue
        tree, conflict = await git_ops.merge_tree_write(repo, current, nxt.result_sha)
        if tree is None:
            _store_input_row(
                db,
                mission_id,
                task_id,
                attempt_number,
                dep_set_json,
                PreparedInput(input_sha=current, input_tree_sha=None, integration_sha=None, dependencies=ordered),
                "CONFLICT",
                f"dependency merge conflict for task {task_id}: {conflict}",
            )
            raise DependencyInputError(
                "CONFLICT",
                f"dependency merge conflict for task {task_id} ({conflict}); "
                "no provider launched, resolve the branches and retry the task",
            )
        parents = [current, nxt.result_sha]
        message = f"orchestrator: dependency input for {task_id}\n\nintegrates (task-ID order): " + ", ".join(
            f"{r.task_id}@{r.result_sha[:8]}" for r in ordered
        )
        merge_sha = await git_ops.commit_tree(repo, tree, parents, message)
        if merge_sha is None:
            _store_input_row(
                db,
                mission_id,
                task_id,
                attempt_number,
                dep_set_json,
                PreparedInput(input_sha=current, input_tree_sha=None, integration_sha=None, dependencies=ordered),
                "FAILED",
                "dependency merge commit failed",
            )
            raise DependencyInputError("INTEGRATION_INVALID", "dependency merge commit failed")
        try:
            record_write(
                db,
                run_id=None,
                mission_id=mission_id,
                task_id=task_id,
                actor_type=ACTOR_SYSTEM,
                actor_detail=f"dependency integration for {task_id}",
                provider=None,
                role="dependency_integration",
                base_sha=current,
                result_sha=merge_sha,
                tree_sha=tree,
                repo_key_value=await repo_identity(repo),
            )
        except Exception:
            logger.debug("dependency integration provenance failed for %s", task_id, exc_info=True)
        current = merge_sha
        merged_any = True

    # Validate the artifact covers every required result (D-06/§66).
    for r in ordered:
        if not await git_ops.is_ancestor(repo, r.result_sha, current) and r.result_sha != current:
            _store_input_row(
                db,
                mission_id,
                task_id,
                attempt_number,
                dep_set_json,
                PreparedInput(input_sha=current, input_tree_sha=None, integration_sha=None, dependencies=ordered),
                "FAILED",
                f"integration artifact missing {r.task_id}@{r.result_sha[:8]}",
            )
            raise DependencyInputError(
                "INTEGRATION_INVALID", f"integration artifact does not contain {r.task_id}; no provider launched"
            )
    tree = await git_ops.tree_sha(repo, current)
    prepared = PreparedInput(
        input_sha=current,
        input_tree_sha=tree,
        integration_sha=current if merged_any else None,
        dependencies=ordered,
    )
    _store_input_row(db, mission_id, task_id, attempt_number, dep_set_json, prepared, "READY", "")
    return prepared


def _stable_json(dep_set: list[dict[str, str]]) -> str:
    return json.dumps(sorted(dep_set, key=lambda d: d["task_id"]), sort_keys=True)


async def _find_reusable_input(db: Any, repo: Path, task_id: str, dep_set_json: str) -> PreparedInput | None:
    """Reuse a previously prepared input for the same dependency SHAs.

    The stored input commit must still exist and still cover every
    dependency result; otherwise preparation runs fresh (never reuse blind).
    """
    from . import git_ops

    rows = db.query(
        "SELECT * FROM task_dependency_inputs WHERE task_id=? AND dependency_set_json=? AND status='READY'"
        " ORDER BY created_at DESC LIMIT 3",
        (task_id, dep_set_json),
    )
    for row in rows:
        input_sha = normalize_sha(row.get("input_sha"))
        if input_sha is None or not await git_ops.commit_exists(repo, input_sha):
            continue
        try:
            stored: list[dict[str, Any]] = json.loads(row.get("dependency_set_json") or "[]")
        except Exception:
            logger.debug("dependency input row unreadable for %s", task_id, exc_info=True)
            continue
        ok = True
        for dep in stored:
            sha = normalize_sha(dep.get("result_sha"))
            if sha is None or not await git_ops.commit_exists(repo, sha):
                ok = False
                break
            if sha != input_sha and not await git_ops.is_ancestor(repo, sha, input_sha):
                ok = False
                break
        if not ok:
            continue
        deps = [
            DependencyResult(task_id=str(d.get("task_id", "")), result_sha=str(d.get("result_sha", ""))) for d in stored
        ]
        integration = normalize_sha(row.get("integration_sha"))
        return PreparedInput(
            input_sha=input_sha,
            input_tree_sha=row.get("input_tree_sha"),
            integration_sha=integration,
            dependencies=deps,
            reused=True,
        )
    return None


def _store_input_row(
    db: Any,
    mission_id: str,
    task_id: str,
    attempt_number: int,
    dep_set_json: str,
    prepared: PreparedInput,
    status: str,
    error: str,
) -> None:
    try:
        db.insert(
            "task_dependency_inputs",
            {
                "id": f"dep-in-{uuid.uuid4().hex[:12]}",
                "task_id": task_id,
                "mission_id": mission_id,
                "attempt_number": int(attempt_number or 0),
                "dependency_set_json": dep_set_json,
                "input_sha": prepared.input_sha,
                "input_tree_sha": prepared.input_tree_sha,
                "integration_sha": prepared.integration_sha,
                "status": status,
                "error": error[:1000],
                "created_at": utcnow().isoformat(),
            },
        )
    except Exception:
        logger.debug("dependency input row insert failed for %s", task_id, exc_info=True)


async def verify_input_covers(db: Any, repo: Path, input_sha: str, dependency_ids_list: list[str]) -> dict[str, bool]:
    """Per-dependency presence proof for context agreement (D-12).

    Returns {dep_task_id: result-ancestor-of-input}. Pure reads; used by the
    Context Compiler integration so handoffs state verified YES/NO.
    """
    from . import git_ops

    verified: dict[str, bool] = {}
    for dep_id in dependency_ids_list:
        dep = db.get("tasks", dep_id)
        result = effective_task_result(db, dep) if dep else None
        if result is None:
            verified[dep_id] = False
            continue
        verified[dep_id] = result == input_sha or await git_ops.is_ancestor(repo, result, input_sha)
    return verified
