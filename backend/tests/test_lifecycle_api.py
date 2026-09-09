"""API tests for the Idea-to-Product endpoints (ASGI transport, fake providers)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from orchestrator.api.app import create_app
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter, PlanProvider, gated_test_plan


@pytest.fixture()
async def lifecycle_client(tmp_path: Path):
    adapters = {
        "fake-planner": PlanProvider("fake-planner", None, gated_test_plan()),
        "fake-a": FakeAdapter("fake-a", ["work"]),
        "fake-b": FakeAdapter("fake-b", ["ok"]),
    }
    names = list(adapters.keys())
    data = {
        "providers": {n: {"enabled": True, "timeout_minutes": 1} for n in names},
        "orchestration": {
            "review_required": True,
            "max_repair_cycles": 1,
            "max_phase_attempts": 2,
            "cooldown_base_seconds": 0.05,
            "cooldown_multiplier": 1.0,
            "cooldown_max_seconds": 0.2,
            "scheduler_tick_seconds": 0.05,
            "max_provider_wait_seconds": 5,
        },
        "priority": {
            "planning": ["fake-planner"],
            "implementation": ["fake-a", "fake-b"],
            "testing": ["fake-a", "fake-b"],
            "review": ["fake-a", "fake-b"],
            "repair": ["fake-a", "fake-b"],
        },
        "lifecycle": {"workspace_root": str(tmp_path / "products")},
        "git": {"auto_checkpoint": True},
    }
    from orchestrator.config import Config

    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db, Config(data), adapters)
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", Config(data), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await orch.shutdown()


async def test_product_project_crud_and_plan(lifecycle_client: httpx.AsyncClient):
    c = lifecycle_client
    resp = await c.post("/api/product-projects", json={"name": "", "idea": "x"})
    assert resp.status_code == 400
    resp = await c.post(
        "/api/product-projects",
        json={"name": "API Product", "idea": "track widgets locally", "constraints": "simple"},
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    resp = await c.get("/api/product-projects")
    assert any(p["id"] == pid for p in resp.json())

    resp = await c.post(f"/api/product-projects/{pid}/plan")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    resp = await c.get(f"/api/product-projects/{pid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "PLAN_READY"
    assert body["plan"]["product_name"] == "Test Product"
    assert len(body["phases"]) == 0  # phases materialize on start
    assert isinstance(body["evidence"], list)


async def test_start_advance_gate_resolve_flow(lifecycle_client: httpx.AsyncClient):
    c = lifecycle_client
    pid = (
        await c.post("/api/product-projects", json={"name": "Gated", "idea": "needs a token"})
    ).json()["id"]
    assert (await c.post(f"/api/product-projects/{pid}/plan")).json()["ok"] is True

    resp = await c.post(f"/api/product-projects/{pid}/start")
    assert resp.status_code == 200, resp.text
    body = (await c.get(f"/api/product-projects/{pid}")).json()
    assert body["state"] in ("EXECUTING", "WAITING_FOR_HUMAN", "REVIEWING", "FINAL_ACCEPTANCE", "DELIVERED")
    assert len(body["phases"]) == 2
    assert len(body["gates"]) == 1
    gate_id = body["gates"][0]["id"]
    assert body["gates"][0]["required_vars"] == ["TEST_TOKEN"]

    # Missing prerequisite: resolution refused.
    resp = await c.post(f"/api/product-projects/{pid}/gates/{gate_id}/resolve", json={"resolution": "done"})
    assert resp.status_code == 400

    # Unknown gate: 404.
    resp = await c.post(f"/api/product-projects/{pid}/gates/nope/resolve", json={"resolution": "x"})
    assert resp.status_code == 404


async def test_plan_revision_validation(lifecycle_client: httpx.AsyncClient):
    c = lifecycle_client
    pid = (await c.post("/api/product-projects", json={"name": "R", "idea": "i"})).json()["id"]
    await c.post(f"/api/product-projects/{pid}/plan")
    plan = (await c.get(f"/api/product-projects/{pid}")).json()["plan"]
    plan["phases"][0]["depends_on"] = ["feature"]
    resp = await c.put(f"/api/product-projects/{pid}/plan", json={"plan": plan, "reason": ""})
    assert resp.status_code == 400
    plan["phases"][0]["depends_on"] = []
    resp = await c.put(f"/api/product-projects/{pid}/plan", json={"plan": plan, "reason": "tweak"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2


async def test_start_requires_plan(lifecycle_client: httpx.AsyncClient):
    c = lifecycle_client
    pid = (await c.post("/api/product-projects", json={"name": "N", "idea": "i"})).json()["id"]
    resp = await c.post(f"/api/product-projects/{pid}/start")
    assert resp.status_code == 409


async def test_waiver_endpoint_validation(lifecycle_client: httpx.AsyncClient):
    c = lifecycle_client
    pid = (await c.post("/api/product-projects", json={"name": "W", "idea": "i"})).json()["id"]
    await c.post(f"/api/product-projects/{pid}/plan")
    # Unknown criterion rejected.
    resp = await c.post(
        f"/api/product-projects/{pid}/waivers",
        json={"target_kind": "criterion", "target_id": "NOPE", "reason": "x"},
    )
    assert resp.status_code == 400
    # Empty reason rejected.
    resp = await c.post(
        f"/api/product-projects/{pid}/waivers",
        json={"target_kind": "criterion", "target_id": "R1-A1", "reason": "  "},
    )
    assert resp.status_code == 400
    # Valid waiver recorded with revision stamp.
    resp = await c.post(
        f"/api/product-projects/{pid}/waivers",
        json={"target_kind": "criterion", "target_id": "R1-A1", "reason": "manual check done"},
    )
    assert resp.status_code == 200, resp.text
    body = (await c.get(f"/api/product-projects/{pid}")).json()
    assert len(body["waivers"]) == 1
    assert body["waivers"][0]["target_id"] == "R1-A1"
    assert body["waivers"][0]["plan_revision"] == 1
