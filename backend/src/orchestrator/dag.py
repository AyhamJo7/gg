"""Task DAG validation.

Deterministic validation of mission task graphs before execution.
Rejects cycles, self-dependencies, missing IDs, duplicates, and invalid scopes.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .models import TaskGraphTask


class DagValidationError(ValueError):
    """Raised when a task DAG fails validation."""

    pass


def validate_task_graph(tasks: list[TaskGraphTask]) -> None:
    """Validate a list of tasks as a valid DAG.

    Raises DagValidationError with a descriptive message on any violation.
    """
    if not tasks:
        raise DagValidationError("task graph is empty")

    ids = {t.id for t in tasks}
    if len(ids) != len(tasks):
        raise DagValidationError("duplicate task IDs detected")

    for task in tasks:
        if task.id in task.dependencies:
            raise DagValidationError(f"task {task.id} has self-dependency")

        for dep in task.dependencies:
            if dep not in ids:
                raise DagValidationError(f"task {task.id} depends on missing task {dep}")

    # Kahn's algorithm for cycle detection
    in_degree: dict[str, int] = {t.id: 0 for t in tasks}
    adj: dict[str, list[str]] = {t.id: [] for t in tasks}

    for task in tasks:
        for dep in task.dependencies:
            adj[dep].append(task.id)
            in_degree[task.id] += 1

    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])
    visited = 0

    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited != len(tasks):
        raise DagValidationError("cycle detected in task graph")

    # Validate workspace scopes are well-formed strings
    for task in tasks:
        for scope in task.workspace_scope:
            if not isinstance(scope, str) or not scope.strip():
                raise DagValidationError(f"task {task.id} has invalid workspace scope: {scope!r}")


def validate_planner_payload(payload: dict[str, Any]) -> list[TaskGraphTask]:
    """Convert and validate a planner JSON payload into TaskGraphTask objects.

    The expected payload format:
    {
        "tasks": [
            {
                "id": "backend-api",
                "title": "Implement backend API",
                "role": "implementation",
                "depends_on": [],
                "workspace_scope": ["backend/**"],
                "preferred_providers": ["opencode", "codex"]
            },
            ...
        ]
    }

    Raises DagValidationError if the payload is malformed.
    """
    if not isinstance(payload, dict):
        raise DagValidationError("planner payload must be a JSON object")

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise DagValidationError("planner payload missing 'tasks' array")

    tasks: list[TaskGraphTask] = []
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise DagValidationError(f"task at index {idx} is not an object")

        tid = raw.get("id")
        if not tid or not isinstance(tid, str):
            raise DagValidationError(f"task at index {idx} missing valid 'id'")
        if tid in seen_ids:
            raise DagValidationError(f"duplicate task id in planner payload: {tid}")
        seen_ids.add(tid)

        role_str = raw.get("role", "implementation")
        from .models import Role

        try:
            role = Role(role_str)
        except ValueError as exc:
            raise DagValidationError(f"task {tid} has invalid role: {role_str}") from exc

        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise DagValidationError(f"task {tid} 'depends_on' must be an array")

        scope = raw.get("workspace_scope", [])
        if not isinstance(scope, list):
            raise DagValidationError(f"task {tid} 'workspace_scope' must be an array")

        providers = raw.get("preferred_providers", [])
        if not isinstance(providers, list):
            raise DagValidationError(f"task {tid} 'preferred_providers' must be an array")

        tasks.append(
            TaskGraphTask(
                id=tid,
                mission_id=raw.get("mission_id", ""),
                title=raw.get("title", tid),
                description=raw.get("description", ""),
                task_type=raw.get("task_type", "implementation"),
                role=role,
                dependencies=[str(d) for d in depends_on],
                preferred_providers=[str(p) for p in providers],
                workspace_scope=[str(s) for s in scope],
                priority=int(raw.get("priority", 0)),
                max_attempts=int(raw.get("max_attempts", 3)),
            )
        )

    # Populate dependents
    task_map = {t.id: t for t in tasks}
    for task in tasks:
        for dep in task.dependencies:
            if dep in task_map:
                task_map[dep].dependents.append(task.id)

    validate_task_graph(tasks)
    return tasks
