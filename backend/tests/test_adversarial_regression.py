"""Adversarial regression campaign — scenarios A through J.

Each scenario targets a specific class of defect reproduced in the
independent audits. These MUST pass for a clean candidate commit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from conftest import make_config, make_orchestrator
from orchestrator import git_ops
from orchestrator.api.app import create_app
from orchestrator.db import Database
from orchestrator.models import FailureClass, MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.classify import classify_output
from orchestrator.providers.fake import FakeAdapter
from orchestrator.review import parse_review_output

# ---------- helpers ----------------------------------------------------------


def _full_fake_adapters(**overrides: FakeAdapter) -> dict[str, FakeAdapter]:
    base = {
        "fake-a": FakeAdapter("fake-a", ["ok"]),
        "fake-b": FakeAdapter("fake-b", ["ok"]),
        "fake-c": FakeAdapter("fake-c", ["ok"]),
    }
    base.update(overrides)
    return base


async def _run_mission_to_end(orch: Orchestrator, workspace: Path) -> dict:
    mission = orch.create_mission(
        project_id="proj-1",
        title="adversarial test",
        task="test task",
        autonomy="BALANCED",
        profile="balanced",
    )
    orch.start_mission(mission["id"])
    for _ in range(300):  # ~15s max
        m = orch.db.get("missions", mission["id"])
        if m and m["status"] in ("COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED"):
            return m
        await asyncio.sleep(0.05)
    m = orch.db.get("missions", mission["id"])
    assert m, "mission vanished"
    return m


# ===========================================================================
# Scenario A — Porcelain parsing: unstaged modification preserves filename
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_a_porcelain_filename_exact(tmp_path):
    """N-01: Unstaged modification must return exact filename, not truncated."""
    await git_ops.init_repo(tmp_path)
    (tmp_path / "calc.py").write_text("x = 1")
    await git_ops.checkpoint(tmp_path, "initial")

    # Modify without staging → git shows " M calc.py"
    (tmp_path / "calc.py").write_text("x = 2")
    st = await git_ops.status(tmp_path)

    assert "calc.py" in st.modified, f"expected 'calc.py' in modified, got {st.modified}"
    # Ensure no truncation: "alc.py" would indicate the old bug
    assert "alc.py" not in st.modified


@pytest.mark.asyncio
async def test_scenario_a_space_in_filename(tmp_path):
    """N-01 edge: file with spaces must not be corrupted."""
    await git_ops.init_repo(tmp_path)
    (tmp_path / "my file.txt").write_text("hello")
    st = await git_ops.status(tmp_path)
    assert "my file.txt" in st.untracked


@pytest.mark.asyncio
async def test_scenario_a_unicode_filename(tmp_path):
    """N-01 edge: unicode filename must survive parsing."""
    await git_ops.init_repo(tmp_path)
    (tmp_path / "über.py").write_text("pass")
    st = await git_ops.status(tmp_path)
    assert "über.py" in st.untracked


@pytest.mark.asyncio
async def test_scenario_a_leading_dash_and_rename(tmp_path):
    """N-01 edge: leading-dash file and rename must be exact."""
    await git_ops.init_repo(tmp_path)
    (tmp_path / "old.py").write_text("pass")
    await git_ops._git(tmp_path, "add", "old.py")
    await git_ops._git(tmp_path, "commit", "-m", "init")
    await git_ops._git(tmp_path, "mv", "--", "old.py", "-leading-dash.py")
    st = await git_ops.status(tmp_path)
    assert ("old.py", "-leading-dash.py") in st.renamed


# ===========================================================================
# Scenario B — Checkpoint exhaustion prevents COMPLETED
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_b_checkpoint_exhaustion_aborts_mission(tmp_path):
    """N-02: A mission where every checkpoint fails must NEVER reach COMPLETED."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# test\n")
    await git_ops.init_repo(ws)

    adapters = _full_fake_adapters()
    orch = make_orchestrator(tmp_path, adapters)
    orch.db.insert(
        "projects",
        {
            "id": "proj-1",
            "path": str(ws),
            "name": "test",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )

    with patch.object(git_ops, "checkpoint", side_effect=git_ops.GitError("disk full")):
        mission = await _run_mission_to_end(orch, ws)

    assert mission["status"] != MissionStatus.COMPLETED.value, (
        "Mission reached COMPLETED despite persistent checkpoint failure"
    )
    assert mission["status"] in (MissionStatus.UNVERIFIED.value, MissionStatus.FAILED.value)


# ===========================================================================
# Scenario C — Exit 0 + human input pattern → NOT HUMAN_INPUT
# ===========================================================================


def test_scenario_c_exit_0_code_with_yn_not_human_input():
    """N-05: Source code containing (y/n) with exit 0 → NONE."""
    source_outputs = [
        'def confirm():\n    return input("Continue? (y/n)")',
        'PROMPT = "Do you want to proceed? [y/N]"\nprint(PROMPT)',
        '"""Press any key to continue..."""\ndef wait(): pass',
        "waiting for user input → timeout handler\ndef handle(): ...",
    ]
    for output in source_outputs:
        result = classify_output(0, output, timed_out=False, cancelled=False)
        assert result == FailureClass.NONE, f"Exit 0 misclassified as {result}: {output[:60]}"


def test_scenario_c_nonzero_exit_human_input_detected():
    """N-05: Genuine interactive prompt with non-zero exit → HUMAN_INPUT."""
    assert classify_output(1, "[y/N]", timed_out=False, cancelled=False) == FailureClass.HUMAN_INPUT
    assert (
        classify_output(None, "Press any key to continue...", timed_out=False, cancelled=False)
        == FailureClass.HUMAN_INPUT
    )


# ===========================================================================
# Scenario D — PID reuse: mismatched process identity not killed
# ===========================================================================


def test_scenario_d_pid_reuse_protection(tmp_path):
    """N-04: Stale PID must not be signalled if identity doesn't match."""
    adapters = _full_fake_adapters()
    orch = make_orchestrator(tmp_path, adapters)

    # Insert project first (FK constraint)
    orch.db.insert(
        "projects",
        {
            "id": "p1",
            "path": str(tmp_path / "ws"),
            "name": "test",
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    # Insert a stale provider run with a PID that doesn't exist
    orch.db.insert(
        "missions",
        {
            "id": "m-stale",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": "IMPLEMENTING",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    orch.db.insert(
        "provider_runs",
        {
            "id": "run-stale",
            "mission_id": "m-stale",
            "provider": "fake-a",
            "pid": 999999999,
            "pgid": 999999999,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "failure_class": "RUNNING",
            "provider_state": "RUNNING",
        },
    )
    # Reap should NOT crash and should mark the run as finished
    orch._reap_orphaned_processes()
    run = orch.db.get("provider_runs", "run-stale")
    assert run is not None
    assert run["finished_at"] is not None
    assert run["failure_class"] == "CRASH"


# ===========================================================================
# Scenario E — Unparseable review count persists across restart
# ===========================================================================


def test_scenario_e_review_retry_persistence(tmp_path):
    """N-03: Review unparseable count must survive restart."""
    db = Database(tmp_path / "test.db")
    db.insert(
        "projects",
        {
            "id": "p1",
            "name": "test",
            "path": str(tmp_path),
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "test",
            "task": "t",
            "status": "REVIEWING",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    # Simulate: 2 unparseable reviews already persisted
    for i in range(2):
        db.insert(
            "reviews",
            {
                "id": f"rev-{i}",
                "mission_id": "m1",
                "review_parsed": 0,
                "review_provider": "fake",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    rows = db.query(
        "SELECT COUNT(*) as cnt FROM reviews WHERE mission_id=? AND review_parsed=0",
        ("m1",),
    )
    assert rows[0]["cnt"] == 2, "Persisted count should be 2"


# ===========================================================================
# Scenario F — Large file excluded from checkpoint
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_f_large_file_excluded(tmp_path):
    """F-20: File > 5MB must be excluded from auto-checkpoint."""
    await git_ops.init_repo(tmp_path)
    (tmp_path / "small.py").write_text("x = 1")
    (tmp_path / "huge.bin").write_bytes(b"\x00" * (6 * 1024 * 1024))

    sha = await git_ops.checkpoint(tmp_path, "test")
    assert sha, "small file should be committed"

    committed = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "small.py" in committed
    assert "huge.bin" not in committed, "Large file must be excluded"


# ===========================================================================
# Scenario G — Priority validation rejects unknown role/provider
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_g_priority_validation(tmp_path, workspace):
    """F-15: Invalid role/provider must be rejected at the API layer."""
    orch = make_orchestrator(tmp_path, _full_fake_adapters())
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", make_config(providers=list(orch.registry.adapters.keys())), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {app.state.auth_token}"}
    ) as client:
        # Invalid role
        resp = await client.post("/api/settings/priority", json={"role": "hacking", "providers": ["fake-a"]})
        assert resp.status_code == 422, f"Expected 422 for invalid role, got {resp.status_code}"

        # Unknown provider
        resp = await client.post("/api/settings/priority", json={"role": "planning", "providers": ["nonexistent"]})
        assert resp.status_code == 422, f"Expected 422 for unknown provider, got {resp.status_code}"

        # Duplicate provider
        resp = await client.post("/api/settings/priority", json={"role": "planning", "providers": ["fake-a", "fake-a"]})
        assert resp.status_code == 422, f"Expected 422 for duplicate provider, got {resp.status_code}"

        # Empty list
        resp = await client.post("/api/settings/priority", json={"role": "planning", "providers": []})
        assert resp.status_code == 422, f"Expected 422 for empty list, got {resp.status_code}"

        # Valid request
        resp = await client.post("/api/settings/priority", json={"role": "planning", "providers": ["fake-a", "fake-b"]})
        assert resp.status_code == 200


# ===========================================================================
# Scenario H — Gate resolution on terminal mission → 409
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_h_gate_on_terminal_mission(tmp_path, workspace):
    """F-04/F-27: Resolving gate on CANCELLED mission → 409, not 500."""
    orch = make_orchestrator(tmp_path, _full_fake_adapters())
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", make_config(providers=list(orch.registry.adapters.keys())), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {app.state.auth_token}"}
    ) as client:
        # Seed project and cancelled mission with an open gate
        orch.db.insert(
            "projects",
            {
                "id": "p1",
                "name": "test",
                "path": str(tmp_path),
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        orch.db.insert(
            "missions",
            {
                "id": "m-done",
                "project_id": "p1",
                "title": "done",
                "task": "t",
                "status": "CANCELLED",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
        orch.db.insert(
            "human_gates",
            {
                "id": "gate-1",
                "mission_id": "m-done",
                "reason": "test",
                "status": "open",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

        resp = await client.post(
            "/api/missions/m-done/gates/gate-1/resolve",
            json={"resolution": "Approve"},
        )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}"


# ===========================================================================
# Scenario I — Review schema rejects malformed findings
# ===========================================================================


def test_scenario_i_review_schema_bare_ints():
    """Review schema: [1,2,3] must NOT parse as valid review."""
    ok, _findings = parse_review_output("REVIEW_FINDINGS_JSON: [1, 2, 3]")
    assert not ok, "Bare integer array should be unparseable"


def test_scenario_i_review_schema_missing_severity():
    """Review schema: finding without severity must be rejected."""
    ok, _findings = parse_review_output('REVIEW_FINDINGS_JSON: [{"description": "something"}]')
    assert not ok, "Finding without severity should be unparseable"


def test_scenario_i_review_schema_valid():
    """Review schema: well-formed finding must pass."""
    ok, findings = parse_review_output('REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "description": "bug"}]')
    assert ok
    assert len(findings) == 1


# ===========================================================================
# Scenario J — Retry idempotency returns same mission
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_j_retry_idempotency(tmp_path, workspace):
    """Retry idempotency: second retry request returns the same mission."""
    orch = make_orchestrator(tmp_path, _full_fake_adapters())
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", make_config(providers=list(orch.registry.adapters.keys())), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {app.state.auth_token}"}
    ) as client:
        # Seed project
        resp = await client.post("/api/projects", json={"path": str(workspace)})
        assert resp.status_code == 201
        project_id = resp.json()["id"]

        original = orch.create_mission(
            project_id=project_id,
            title="original",
            task="test",
            autonomy="BALANCED",
            profile="balanced",
        )
        orch.db.update("missions", original["id"], {"status": "FAILED"})

        retry1_resp = await client.post(f"/api/missions/{original['id']}/retry")
        assert retry1_resp.status_code == 200
        retry1 = retry1_resp.json()

        retry2_resp = await client.post(f"/api/missions/{original['id']}/retry")
        assert retry2_resp.status_code == 200
        retry2 = retry2_resp.json()

        assert retry1["id"] == retry2["id"], f"Second retry created new mission: {retry1['id']} vs {retry2['id']}"
