"""Manual DAG endpoint: globally-unique task IDs, strict validation, idempotent resubmit."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_config, make_orchestrator
from orchestrator.api.app import create_app
from orchestrator.dag import namespace_dag_ids
from orchestrator.models import TaskGraphTask
from orchestrator.providers.fake import FakeAdapter
from orchestrator.readiness import compute_ready_tasks


@pytest.fixture()
async def client(tmp_path: Path, workspace: Path):
    orch = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", make_config(providers=["fake-a"]), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.orchestrator = orch  # type: ignore[attr-defined]
        yield c
    await orch.shutdown()


def _dag(tasks=("task-a", "task-b"), deps=(), scopes=("src/a.py", "src/b.py")):
    return {
        "tasks": [
            {
                "id": tid,
                "role": "implementation",
                "title": tid,
                "description": f"do {tid}",
                "preferred_providers": '["fake-a"]',
                "workspace_scope": f'["{scopes[i % len(scopes)]}"]',
                "priority": 0,
            }
            for i, tid in enumerate(tasks)
        ],
        "dependencies": [
            {"from_task_id": a, "to_task_id": b} for a, b in deps
        ],
    }


async def _mission(client: httpx.AsyncClient, workspace: Path, title: str) -> dict:
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    resp = await client.post(
        "/api/missions",
        json={
            "project_id": project["id"],
            "title": title,
            "task": "do things",
            "autonomy": "AUTONOMOUS",
            "scheduling_mode": "PARALLEL_SAFE",
            "start": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_namespace_rewrites_ids_and_deps():
    tasks = [
        TaskGraphTask(id="a", mission_id="m", dependencies=[]),
        TaskGraphTask(id="b", mission_id="m", dependencies=["a"]),
    ]
    mapping = namespace_dag_ids("mission123456", tasks)
    assert mapping == {"a": "mission1-a", "b": "mission1-b"}
    assert tasks[0].id == "mission1-a"
    assert tasks[1].dependencies == ["mission1-a"]


async def test_same_logical_ids_across_missions_do_not_collide(
    client: httpx.AsyncClient, workspace: Path
):
    m1 = await _mission(client, workspace, "m1")
    m2 = await _mission(client, workspace, "m2")
    r1 = await client.post(f"/api/missions/{m1['id']}/dag", json=_dag())
    assert r1.status_code == 200, r1.text
    r2 = await client.post(f"/api/missions/{m2['id']}/dag", json=_dag())
    assert r2.status_code == 200, r2.text

    dag1 = (await client.get(f"/api/missions/{m1['id']}/dag")).json()
    dag2 = (await client.get(f"/api/missions/{m2['id']}/dag")).json()
    ids1 = {t["id"] for t in dag1["tasks"]}
    ids2 = {t["id"] for t in dag2["tasks"]}
    assert len(ids1) == 2 and len(ids2) == 2
    assert not ids1 & ids2
    assert all(t["status"] == "PENDING" for t in dag1["tasks"] + dag2["tasks"])


async def test_dag_resubmit_is_idempotent(client: httpx.AsyncClient, workspace: Path):
    m = await _mission(client, workspace, "m-retry")
    assert (await client.post(f"/api/missions/{m['id']}/dag", json=_dag())).status_code == 200
    assert (await client.post(f"/api/missions/{m['id']}/dag", json=_dag())).status_code == 200
    dag = (await client.get(f"/api/missions/{m['id']}/dag")).json()
    assert len(dag["tasks"]) == 2


async def test_manual_tasks_are_schedulable(client: httpx.AsyncClient, workspace: Path):
    m = await _mission(client, workspace, "m-ready")
    assert (await client.post(f"/api/missions/{m['id']}/dag", json=_dag())).status_code == 200
    ready = compute_ready_tasks(client.orchestrator.db, m["id"])
    assert {t["id"] for t in ready} == {
        t["id"] for t in (await client.get(f"/api/missions/{m['id']}/dag")).json()["tasks"]
    }


async def test_same_scope_tasks_serialize_on_locks(client: httpx.AsyncClient, workspace: Path):
    m = await _mission(client, workspace, "m-locks")
    payload = _dag(scopes=("src/shared.py", "src/shared.py"))
    assert (await client.post(f"/api/missions/{m['id']}/dag", json=payload)).status_code == 200
    ready = compute_ready_tasks(client.orchestrator.db, m["id"])
    assert len(ready) == 1


async def test_invalid_dags_rejected(client: httpx.AsyncClient, workspace: Path):
    m = await _mission(client, workspace, "m-bad")
    # unknown dependency target
    r = await client.post(
        f"/api/missions/{m['id']}/dag",
        json=_dag(deps=[("task-a", "nope")]),
    )
    assert r.status_code == 400, r.text
    # duplicate ids
    r = await client.post(f"/api/missions/{m['id']}/dag", json=_dag(tasks=("dup", "dup")))
    assert r.status_code == 400, r.text
    # cycle
    r = await client.post(
        f"/api/missions/{m['id']}/dag",
        json=_dag(deps=[("task-a", "task-b"), ("task-b", "task-a")]),
    )
    assert r.status_code == 400, r.text
    # empty
    r = await client.post(f"/api/missions/{m['id']}/dag", json={"tasks": [], "dependencies": []})
    assert r.status_code == 400, r.text
