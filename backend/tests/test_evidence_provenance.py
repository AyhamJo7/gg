"""Exact-SHA evidence + attempt lineage + writer provenance (Increment 3, P-01..P-30).

Part 1: engine-driven writer/review/attempt behavior with fake providers.
Part 2 (below): deterministic evaluate_artifact_evidence scenarios on real git repos.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from conftest import make_config, make_orchestrator
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.provenance import (
    ACTOR_HUMAN,
    ACTOR_PROVIDER,
    adopt_head_as_human,
    begin_phase_attempt,
    finish_phase_attempt,
    mission_provider_writers,
)
from orchestrator.providers.fake import FakeAdapter


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


async def _run_mission(orch: Orchestrator, mission_id: str, timeout: float = 60) -> None:
    orch.start_mission(mission_id)
    await asyncio.wait_for(orch._engine_tasks[mission_id], timeout=timeout)


def _seed_project(orch: Orchestrator, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


# -- writers ---------------------------------------------------------------


def test_single_writer_bound_to_exact_sha(tmp_path: Path, workspace: Path):
    """P-01/P-02: implementation run links base_sha -> checkpoint result_sha."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        impl_runs = orch.db.query(
            "SELECT * FROM provider_runs WHERE mission_id=? AND stage='implementation'", (mission["id"],)
        )
        assert len(impl_runs) == 1
        run = impl_runs[0]
        rows = orch.db.query("SELECT * FROM write_provenance WHERE run_id=?", (run["id"],))
        assert len(rows) == 1, "exactly one write row per code-writing run"
        w = rows[0]
        assert w["actor_type"] == ACTOR_PROVIDER
        assert w["provider"] == run["provider"]
        assert w["base_sha"] == run["git_commit_before"]
        assert w["result_sha"]
        # Result SHA exists in the repo and matches post-run HEAD lineage.
        head = _git(workspace, "rev-parse", "HEAD")
        assert _git(workspace, "merge-base", "--is-ancestor", w["result_sha"], head) == ""
        await orch.shutdown()

    asyncio.run(main())


def test_repair_joins_writer_set(tmp_path: Path, workspace: Path):
    """P-04/P-10: a repairer that writes becomes a candidate writer."""

    async def main() -> None:
        adapters = {
            "fake-a": FakeAdapter("fake-a", ["work", "work"]),
            "fake-b": FakeAdapter("fake-b", ["work", "work", "work"]),
        }
        cfg = make_config(providers=["fake-a", "fake-b"])
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        # Force a repair cycle: reviewer flags, repairer writes.
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        writers, complete = mission_provider_writers(orch.db, mission["id"])
        assert complete, "fake flow writes are fully attributed"
        assert writers, "writer set must not be empty after writes"
        # Every tracked run has a write row (no silent gaps).
        runs = orch.db.query(
            "SELECT id FROM provider_runs WHERE mission_id=? AND failure_class='NONE'"
            " AND stage IN ('mission_plan','implementation','repair','task','testing')",
            (mission["id"],),
        )
        linked = orch.db.query(
            "SELECT COUNT(*) as n FROM write_provenance WHERE mission_id=? AND run_id IS NOT NULL",
            (mission["id"],),
        )
        assert linked[0]["n"] >= len(runs), f"runs={len(runs)} linked={linked[0]['n']}"
        await orch.shutdown()

    asyncio.run(main())


def test_retry_chain_links_runs(tmp_path: Path, workspace: Path):
    """P-19: failover attempts form a retry_of_run_id chain, nothing overwritten."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ratelimit", "ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        planning = orch.db.query(
            "SELECT id, retry_of_run_id, failure_class FROM provider_runs WHERE mission_id=? AND stage='mission_plan'"
            " ORDER BY started_at ASC",
            (mission["id"],),
        )
        assert len(planning) >= 2, "failover must preserve both attempts"
        assert planning[0]["retry_of_run_id"] is None
        assert planning[1]["retry_of_run_id"] == planning[0]["id"], "retry chain must link"
        await orch.shutdown()

    asyncio.run(main())


def test_dirty_workspace_owned_as_human_at_analyze(tmp_path: Path, workspace: Path):
    """P-03/P-06: pre-existing dirt is checkpointed as HUMAN at analyze time —
    never attributed to the provider."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "t@t")
        _git(workspace, "config", "user.name", "t")
        _git(workspace, "add", ".")
        _git(workspace, "commit", "-m", "init")
        (workspace / "human_edit.txt").write_text("operator was here\n")
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        # Analyze owns pre-existing dirt as HUMAN; the mission can proceed.
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        human_rows = orch.db.query(
            "SELECT * FROM write_provenance WHERE mission_id=? AND actor_type='HUMAN_OPERATOR'", (mission["id"],)
        )
        assert len(human_rows) == 1, "pre-existing changes get exactly one HUMAN row"
        # ...and no provider row claims to have PRODUCED the human commit
        # (no-change rows legitimately reference the same HEAD as base==result).
        human_sha = human_rows[0]["result_sha"]
        producers = orch.db.query(
            "SELECT base_sha FROM write_provenance WHERE mission_id=? AND actor_type='PROVIDER' AND result_sha=?",
            (mission["id"], human_sha),
        )
        assert producers, "expected rows referencing the human HEAD"
        assert all(p["base_sha"] == human_sha for p in producers), (
            "only no-change provider rows may reference the human commit"
        )
        await orch.shutdown()

    asyncio.run(main())


def test_dirty_workspace_no_checkpoint_backend(tmp_path: Path, workspace: Path):
    """P-03: with auto-checkpoint disabled, unattributed dirt fails closed pre-execution."""
    from conftest import make_config as _make_config

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        cfg = _make_config(providers=["fake-a"])
        cfg.raw["git"] = {"auto_checkpoint": False}
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "t@t")
        _git(workspace, "config", "user.name", "t")
        _git(workspace, "add", ".")
        _git(workspace, "commit", "-m", "init")
        (workspace / "uncommitted.txt").write_text("dirt\n")
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert "unattributed changes" in (final.get("blocking_issue") or "")
        assert all(a.calls == 0 for a in adapters.values())
        assert orch.db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0
        await orch.shutdown()

    asyncio.run(main())


def test_capture_write_start_filters_bookkeeping(tmp_path: Path):
    """P-03 unit: .orchestrator noise and oversized untracked files never block."""
    import asyncio as _asyncio

    from orchestrator.provenance import capture_write_start

    async def main() -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _git(ws, "init")
        _git(ws, "config", "user.email", "t@t")
        _git(ws, "config", "user.name", "t")
        (ws / "README.md").write_text("# t")
        _git(ws, "add", ".")
        _git(ws, "commit", "-m", "init")
        base, dirty, _ = await capture_write_start(ws)
        assert base and not dirty
        (ws / ".orchestrator").mkdir()
        (ws / ".orchestrator" / "logs").mkdir()
        (ws / ".orchestrator" / "logs" / "run.log").write_text("noise")
        (ws / "dataset.bin").write_bytes(b"\0" * (6 * 1024 * 1024))
        _, dirty2, _ = await capture_write_start(ws)
        assert not dirty2, "bookkeeping + oversized files must not block"
        (ws / "real_edit.txt").write_text("human\n")
        _, dirty3, paths = await capture_write_start(ws)
        assert dirty3 and paths == ["real_edit.txt"]

    _asyncio.run(main())


def test_human_adopt_flow_then_rerun(tmp_path: Path, workspace: Path):
    """P-06/§60: adopted human changes unblock; artifact carries HUMAN_OPERATOR."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "t@t")
        _git(workspace, "config", "user.name", "t")
        _git(workspace, "add", ".")
        _git(workspace, "commit", "-m", "init")
        (workspace / "human_edit.txt").write_text("operator was here\n")
        result = await adopt_head_as_human(orch.db, workspace, mission_id="m-adopt")
        assert result["adopted"] is True
        assert result["result_sha"]
        rows = orch.db.query("SELECT * FROM write_provenance WHERE run_id IS NULL AND mission_id='m-adopt'")
        assert len(rows) == 1 and rows[0]["actor_type"] == ACTOR_HUMAN
        # Clean again → provider work proceeds and does not absorb the human row.
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        await orch.shutdown()

    asyncio.run(main())


def test_phase_attempts_preserved(tmp_path: Path):
    """P-18: attempt numbers are stable, monotonic, unique per phase."""
    db_path = tmp_path / "a.db"
    from orchestrator.db import Database

    db = Database(db_path)
    first = begin_phase_attempt(db, "phase-1", "m-1", trigger="INITIAL")
    assert first["attempt_number"] == 1
    finish_phase_attempt(db, first["id"], "FAILED")
    second = begin_phase_attempt(db, "phase-1", "m-2", trigger="PHASE_RETRY")
    assert second["attempt_number"] == 2
    assert second["id"] != first["id"]
    finish_phase_attempt(db, second["id"], "COMPLETED", result_sha="a" * 40)
    rows = db.query("SELECT * FROM project_phase_attempts WHERE phase_id=? ORDER BY attempt_number ASC", ("phase-1",))
    assert [(r["attempt_number"], r["status"], r["mission_id"]) for r in rows] == [
        (1, "FAILED", "m-1"),
        (2, "COMPLETED", "m-2"),
    ], "failed attempts are preserved, never mutated into success"
    db.close()


def test_flagship_review_repair_rereview_lineage(tmp_path: Path, workspace: Path):
    """P-07/P-09/P-10/P-20/P-21/P-22 flagship: implement A -> review finds ->
    repair B -> stale until independent re-review of B passes."""
    import json as _json

    from orchestrator.providers.fake import FindingsProvider
    from orchestrator.review import finding_fingerprint

    finding = {
        "severity": "HIGH",
        "category": "correctness",
        "file": "src/a.ts",
        "description": "whitespace titles accepted",
        "recommended_fix": "return 400",
    }
    fp = finding_fingerprint("HIGH", "correctness", "src/a.ts", "whitespace titles accepted")
    reviewer = FindingsProvider(
        "fake-rev",
        findings_script=[[finding], []],
        verified_script=[[], [{"fingerprint": fp, "evidence": "probe passes at HEAD"}]],
    )

    async def main() -> None:
        adapters = {
            "fake-impl": FakeAdapter("fake-impl", ["work", "work", "work", "work"]),
            "fake-rev": reviewer,
            "fake-rep": FakeAdapter("fake-rep", ["work", "work"]),
        }
        cfg = make_config(providers=["fake-impl", "fake-rev", "fake-rep"])
        priorities = cfg.raw.setdefault("priority", {})
        priorities.update(
            {
                "planning": ["fake-impl"],
                "implementation": ["fake-impl"],
                "testing": ["fake-impl"],
                "review": ["fake-rev"],
                "repair": ["fake-rep"],
            }
        )
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        db = orch.db
        assert db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value

        reviews = db.query("SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at ASC", (mission["id"],))
        assert len(reviews) == 2, f"repair must force a second review, got {len(reviews)}"
        first, second = reviews
        assert first["reviewed_head_sha"] != second["reviewed_head_sha"], "re-review must cover the repaired SHA"
        assert first["independent"] == 1 and second["independent"] == 1
        assert _json.loads(second["writer_set_json"]) == ["fake-impl", "fake-rep"] or set(
            _json.loads(second["writer_set_json"])
        ) >= {"fake-impl", "fake-rep"}

        # The repair run binds base(A-era) -> result(B).
        repairs = db.query("SELECT * FROM provider_runs WHERE mission_id=? AND role='repair'", (mission["id"],))
        assert len(repairs) == 1
        wrows = db.query("SELECT * FROM write_provenance WHERE run_id=?", (repairs[0]["id"],))
        assert len(wrows) == 1
        assert wrows[0]["base_sha"] == repairs[0]["git_commit_before"]
        assert wrows[0]["result_sha"] != first["reviewed_head_sha"], "repair must produce a new SHA"

        # Finding lineage: origin review 1, verified resolution in review 2.
        findings = db.query("SELECT * FROM review_findings WHERE mission_id=?", (mission["id"],))
        assert len(findings) == 1
        f = findings[0]
        assert f["status"] == "resolved"
        assert f["origin_review_id"] == first["id"], "origin preserved, not rewritten"
        assert f["origin_sha"] == first["reviewed_head_sha"]
        assert f["resolved_review_id"] == second["id"]
        assert f["resolved_sha"] == second["reviewed_head_sha"]
        await orch.shutdown()

    asyncio.run(main())


def test_fresh_checkout_rejects_uncommitted_dependency(tmp_path: Path):
    """P-15/§93 adversarial: tests pass in the workdir via an artifact that
    can never be part of the commit (.orchestrator/ is checkpoint-excluded)
    -> fresh clone must fail and delivery must block."""
    import json as _json

    from conftest import make_orchestrator as _make_orch

    async def main() -> None:
        orch = _make_orch(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
        repo = tmp_path / "victim"
        repo.mkdir()
        (repo / "package.json").write_text(_json.dumps({"name": "victim", "scripts": {"test": "node check.js"}}))
        (repo / "check.js").write_text("require('./.orchestrator/hidden-lib.js');\nconsole.log('LOCAL_OK');\n")
        (repo / ".orchestrator").mkdir()
        (repo / ".orchestrator" / "hidden-lib.js").write_text("module.exports = {};\n")
        # GG checkpoint policy excludes .orchestrator/ from every commit, so a
        # workdir-only artifact there can never reach the clone.
        (repo / ".gitignore").write_text(".orchestrator/\n")
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")
        sha = _git(repo, "rev-parse", "HEAD")
        # Workdir passes (hidden artifact present locally)...
        local = await asyncio.to_thread(subprocess.run, ["node", "check.js"], cwd=repo, capture_output=True, text=True)
        assert local.returncode == 0, "workdir precondition must pass"
        # ...but the fresh clone cannot reproduce it.
        ok, detail = await orch.coordinator._fresh_checkout_verify(repo, sha)
        assert ok is False, f"fresh checkout must fail without the uncommitted artifact: {detail}"
        assert "check.js" in detail or "hidden" in detail or "Cannot find" in detail or "FAIL" in detail.upper()
        await orch.shutdown()

    asyncio.run(main())


def test_post_review_human_edit_stales_everything(tmp_path: Path):
    """P-09/P-12/P-13/P-15/§92: human checkpoint after evidence -> all stale."""
    import asyncio as _asyncio

    from orchestrator.provenance import EvidenceInputs, PhaseCandidate, evaluate_artifact_evidence

    ws = tmp_path / "repo"
    ws.mkdir()
    _git(ws, "init")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    (ws / "app.txt").write_text("v1\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "impl")
    sha_a = _git(ws, "rev-parse", "HEAD")

    from orchestrator.db import Database as _Database

    db = _Database(tmp_path / "h.db")
    db.insert("projects", {"id": "proj-1", "name": "p", "path": str(ws), "detected_type": "node", "created_at": "t"})
    db.insert(
        "missions",
        {
            "id": "m-1",
            "project_id": "proj-1",
            "title": "t",
            "task": "t",
            "status": "COMPLETED",
            "created_at": "t",
            "updated_at": "t",
        },
    )
    db.insert(
        "write_provenance",
        {
            "id": "w-1",
            "run_id": "run-1",
            "mission_id": "m-1",
            "task_id": None,
            "product_project_id": None,
            "phase_id": None,
            "actor_type": "PROVIDER",
            "actor_detail": "",
            "provider": "opencode",
            "role": "implementation",
            "base_sha": None,
            "result_sha": sha_a,
            "tree_sha": None,
            "dirty_before": 0,
            "repo_key": "",
            "created_at": "t",
        },
    )
    db.insert(
        "reviews",
        {
            "id": "rev-1",
            "mission_id": "m-1",
            "implementation_provider": "opencode",
            "review_provider": "agy",
            "independent": 1,
            "degradation_reason": None,
            "review_parsed": 1,
            "reviewed_base_sha": sha_a,
            "reviewed_head_sha": sha_a,
            "writer_set_json": '["opencode"]',
            "created_at": "t",
        },
    )
    db.insert(
        "verification_attempts",
        {
            "id": "ver-1",
            "mission_id": "m-1",
            "product_project_id": None,
            "task_id": None,
            "sha": sha_a,
            "repo_key": "",
            "kind": "toolchain",
            "commands_json": "[]",
            "status": "passed",
            "exit_code": 0,
            "started_at": "t",
            "finished_at": "t",
            "summary": "",
        },
    )
    # Human edits + explicitly adopts -> new SHA B.
    (ws / "app.txt").write_text("v1 human tweak\n")

    async def main() -> None:
        result = await adopt_head_as_human(db, ws, mission_id="m-1")
        assert result["adopted"] is True
        sha_b = result["result_sha"]
        assert sha_b != sha_a
        out = await evaluate_artifact_evidence(
            db,
            EvidenceInputs(
                project_id="none",
                candidate_sha=sha_b,
                plan_revision=0,
                repo=ws,
                phases=[PhaseCandidate(phase_id="p", candidate_sha=sha_b, mission_id="m-1")],
                oldest_base_sha=sha_a,
            ),
        )
        # Review/verification at A cannot prove B; writers stay complete via HUMAN row.
        assert out["review"]["state"] in ("STALE", "MISSING", "FAILED"), out["review"]
        assert out["verification"]["state"] == "MISSING"
        assert out["writers_complete"], out["blocking_reasons"]
        assert not out["delivery_ready"]

    _asyncio.run(main())
    db.close()


def test_criterion_recheck_creates_new_attempt(tmp_path: Path):
    """P-14: explicit RECHECK executes anew with recheck_of lineage; the
    default path reuses valid cached evidence without duplicating runs."""
    import asyncio as _asyncio
    import json as _json

    from conftest import make_orchestrator as _make_orch
    from orchestrator.product_plan import ProductPlan as _ProductPlan
    from orchestrator.providers.fake import default_test_plan as _default_plan

    async def main() -> None:
        orch = _make_orch(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
        project = orch.coordinator.create_project("P", "idea", "", False, True, "")
        pid = project["id"]
        repo = tmp_path / "crit-repo"
        repo.mkdir()
        (repo / "package.json").write_text(
            _json.dumps({"name": "c", "scripts": {"probe": 'node -e "process.exit(0)"'}})
        )
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")
        sha = _git(repo, "rev-parse", "HEAD")
        plan_dict = _default_plan()
        for req in plan_dict["requirements"]:
            for criterion in req["acceptance"]:
                criterion["verify"] = "npm run probe"
        plan = _ProductPlan(**plan_dict)
        coord = orch.coordinator
        assert await coord._evaluate_requirement_criteria_locked(pid, plan, repo, sha) == []
        assert await coord._evaluate_requirement_criteria_locked(pid, plan, repo, sha) == []
        rows = orch.db.query("SELECT * FROM criterion_attempts WHERE project_id=? ORDER BY created_at ASC", (pid,))
        assert len(rows) == 2, "one attempt per criterion, cache hit creates none"
        assert await coord._evaluate_requirement_criteria_locked(pid, plan, repo, sha, force=True) == []
        rows2 = orch.db.query("SELECT * FROM criterion_attempts WHERE project_id=? ORDER BY created_at ASC", (pid,))
        assert len(rows2) == 4, "recheck executes anew"
        assert rows2[2]["recheck_of"] == rows2[0]["id"]
        assert rows2[3]["recheck_of"] == rows2[1]["id"]
        await orch.shutdown()

    _asyncio.run(main())


def test_evidence_and_adopt_endpoints(tmp_path: Path, workspace: Path):
    """Evidence API is read-only metadata; adopt checkpoints dirt as HUMAN."""
    import httpx

    from orchestrator.api.app import create_app

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        app = create_app(tmp_path / "api.db", make_config(providers=["fake-a"]), orch)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {app.state.auth_token}"},
        ) as client:
            # Unknown product -> 404, no leakage.
            resp = await client.get("/api/product-projects/nope/evidence")
            assert resp.status_code == 404
            # Mission-scoped adopt: dirty tree adopted as HUMAN.
            _git(workspace, "init")
            _git(workspace, "config", "user.email", "t@t")
            _git(workspace, "config", "user.name", "t")
            _git(workspace, "add", ".")
            _git(workspace, "commit", "-m", "init")
            (workspace / "adhoc.txt").write_text("operator note\n")
            _seed_project(orch, workspace)
            mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
            resp = await client.post(f"/api/missions/{mission['id']}/adopt-changes", json={})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["adopted"] is True
            rows = orch.db.query(
                "SELECT * FROM write_provenance WHERE mission_id=? AND actor_type='HUMAN_OPERATOR'",
                (mission["id"],),
            )
            assert len(rows) == 1
            # Clean tree adopt is a no-op (no fake commit).
            resp2 = await client.post(f"/api/missions/{mission['id']}/adopt-changes", json={})
            assert resp2.status_code == 200
            assert resp2.json()["adopted"] is False
            # Unknown mission -> 404.
            resp3 = await client.post("/api/missions/nope/adopt-changes", json={})
            assert resp3.status_code == 404
        await orch.shutdown()

    asyncio.run(main())
