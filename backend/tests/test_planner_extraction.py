"""Regression tests for planner DAG extraction."""
from __future__ import annotations

from orchestrator.parallel_engine import ParallelMissionEngine


class _FakeEngine:
    """Minimal stand-in to access the static extraction helper."""

    _extract_dag_from_output = ParallelMissionEngine._extract_dag_from_output


EXTRACT = _FakeEngine._extract_dag_from_output


PLAIN_JSON = '{"tasks": [{"id": "a", "title": "A", "role": "implementation", "depends_on": [], "workspace_scope": ["src/*"], "preferred_providers": ["agy"]}]}'

FENCED_JSON = """Here is the plan:

```json
{"tasks": [{"id": "a", "title": "A", "role": "implementation", "depends_on": [], "workspace_scope": ["src/*"], "preferred_providers": ["agy"]}]}
```

Hope this helps!
"""

FENCED_NO_LANG = """```
{"tasks": [{"id": "a", "title": "A", "role": "implementation", "depends_on": [], "workspace_scope": ["src/*"], "preferred_providers": ["agy"]}]}
```
"""

SURROUNDING_PROSE = """I analyzed the mission and here is the breakdown.

{"tasks": [{"id": "a", "title": "A", "role": "implementation", "depends_on": [], "workspace_scope": ["src/*"], "preferred_providers": ["agy"]}]}

Let me know if you need changes.
"""

MALFORMED_JSON = "{not valid json}"

MISSING_TASKS = '{"phases": [{"id": "a"}]}'

NESTED_OBJECTS = '{"meta": {"count": 1}, "tasks": [{"id": "a", "title": "A", "role": "implementation", "depends_on": [], "workspace_scope": ["src/*"], "preferred_providers": ["agy"]}]}'

MULTIPLE_BLOCKS = """Some intro.
```json
{"tasks": [{"id": "a", "title": "A", "role": "implementation", "depends_on": [], "workspace_scope": ["src/*"], "preferred_providers": ["agy"]}]}
```
And then another object {"tasks": []} that should be ignored.
"""

EMPTY_TASKS = '{"tasks": []}'


def test_plain_json():
    result = EXTRACT(PLAIN_JSON)
    assert result is not None
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["id"] == "a"


def test_fenced_json():
    result = EXTRACT(FENCED_JSON)
    assert result is not None
    assert len(result["tasks"]) == 1


def test_fenced_no_lang():
    result = EXTRACT(FENCED_NO_LANG)
    assert result is not None
    assert len(result["tasks"]) == 1


def test_surrounding_prose():
    result = EXTRACT(SURROUNDING_PROSE)
    assert result is not None
    assert len(result["tasks"]) == 1


def test_malformed_json_returns_none():
    result = EXTRACT(MALFORMED_JSON)
    assert result is None


def test_missing_tasks_returns_none():
    result = EXTRACT(MISSING_TASKS)
    assert result is None


def test_nested_objects():
    result = EXTRACT(NESTED_OBJECTS)
    assert result is not None
    assert result["meta"]["count"] == 1
    assert len(result["tasks"]) == 1


def test_multiple_blocks_uses_first_valid():
    result = EXTRACT(MULTIPLE_BLOCKS)
    assert result is not None
    assert len(result["tasks"]) == 1


def test_empty_tasks_is_valid():
    result = EXTRACT(EMPTY_TASKS)
    assert result is not None
    assert result["tasks"] == []
