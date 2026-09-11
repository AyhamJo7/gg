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
        # S-10: provenance tracking adds zero provider calls — exactly the
        # four phase executions, each with exactly one durable run.
        total_calls = sum(a.calls for a in adapters.values())
        total_runs = orch.db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"]
        assert total_calls == total_runs == 4, (total_calls, total_runs)
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


def test_dirty_workspace_blocks_start_without_commit(tmp_path: Path, workspace: Path):
    """F-PROV-03/P-36/S-03/S-04/§17: dirty mission start stages/commits
    nothing, launches nothing, and waits honestly — then explicit adoption
    unblocks with HUMAN_OPERATOR provenance for the new SHA."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        _git(workspace, "init")
        _git(workspace, "config", "user.email", "t@t")
        _git(workspace, "config", "user.name", "t")
        _git(workspace, "add", ".")
        _git(workspace, "commit", "-m", "init")
        head_a = _git(workspace, "rev-parse", "HEAD")
        (workspace / "README.md").write_text("# operator edit\n")
        (workspace / "notes.txt").write_text("untracked operator note\n")
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        orch.start_mission(mission["id"])
        engine_task = orch._engine_tasks[mission["id"]]
        # Wait for the honest block (bounded; never hangs the suite).
        for _ in range(200):
            row = orch.db.get("missions", mission["id"])
            if row["status"] == MissionStatus.WAITING_FOR_HUMAN.value:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("mission did not block for operator decision")
        # Proof of no automatic adoption: nothing executed, HEAD pinned,
        # contents untouched, zero provenance rows.
        assert all(a.calls == 0 for a in adapters.values()), "provider execute calls = 0"
        assert orch.db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0
        assert _git(workspace, "rev-parse", "HEAD") == head_a, "HEAD remains A"
        assert (workspace / "README.md").read_text() == "# operator edit\n", "X untouched"
        assert (workspace / "notes.txt").read_text() == "untracked operator note\n", "Y untouched"
        assert orch.db.query("SELECT COUNT(*) as n FROM write_provenance")[0]["n"] == 0, "no HUMAN row"
        gates = orch.db.query("SELECT * FROM human_gates WHERE mission_id=? AND status='open'", (mission["id"],))
        assert gates, "explicit operator gate required"
        assert "nattribut" in (gates[0]["reason"] + gates[0]["detail"]), "reason must explain attribution"
        # Operator explicitly adopts -> new SHA B with HUMAN_OPERATOR row.
        adopted = await adopt_head_as_human(orch.db, workspace, mission_id=mission["id"])
        assert adopted["adopted"] is True
        head_b = adopted["result_sha"]
        assert head_b != head_a
        human_rows = orch.db.query(
            "SELECT * FROM write_provenance WHERE mission_id=? AND actor_type='HUMAN_OPERATOR'", (mission["id"],)
        )
        assert len(human_rows) == 1 and human_rows[0]["result_sha"] == head_b
        # Resolve the gate -> mission resumes and completes on clean tree.
        orch.resolve_gate(gates[0]["id"], "Adopted/committed/cleaned — continue")
        await asyncio.wait_for(engine_task, timeout=60)
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        await orch.shutdown()

    asyncio.run(main())


def test_dirty_workspace_no_checkpoint_backend(tmp_path: Path, workspace: Path):
    """P-03: with auto-checkpoint disabled, unattributed dirt still blocks
    via gate (never fails closed into silent legacy execution)."""
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
        orch.start_mission(mission["id"])
        for _ in range(200):
            row = orch.db.get("missions", mission["id"])
            if row["status"] == MissionStatus.WAITING_FOR_HUMAN.value:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("mission did not block for operator decision")
        assert all(a.calls == 0 for a in adapters.values())
        assert orch.db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0
        assert orch.db.query("SELECT COUNT(*) as n FROM write_provenance")[0]["n"] == 0
        orch.cancel_mission(mission["id"])
        await asyncio.wait_for(orch._engine_tasks[mission["id"]], timeout=30)
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
        # Order by rowid (insertion sequence): created_at ties on coarse
        # clocks, and per-criterion lineage must follow attempt order.
        rows = orch.db.query("SELECT * FROM criterion_attempts WHERE project_id=? ORDER BY rowid ASC", (pid,))
        assert len(rows) == 2, "one attempt per criterion, cache hit creates none"
        by_criterion: dict[str, list[dict[str, object]]] = {}
        for r in rows:
            by_criterion.setdefault(str(r["criterion_id"]), []).append(r)
        assert await coord._evaluate_requirement_criteria_locked(pid, plan, repo, sha, force=True) == []
        rows2 = orch.db.query("SELECT * FROM criterion_attempts WHERE project_id=? ORDER BY rowid ASC", (pid,))
        assert len(rows2) == 4, "recheck executes anew"
        by_criterion2: dict[str, list[dict[str, object]]] = {}
        for r in rows2:
            by_criterion2.setdefault(str(r["criterion_id"]), []).append(r)
        assert set(by_criterion2) == set(by_criterion) and all(len(v) == 2 for v in by_criterion2.values())
        for cid, attempts in by_criterion2.items():
            assert attempts[1]["recheck_of"] == attempts[0]["id"], f"{cid} recheck must link prior attempt"
        await orch.shutdown()

    _asyncio.run(main())


def test_delivery_race_candidate_change_blocks(tmp_path: Path):
    """F-PROV-04/P-34/S-07/S-08/S-09/§21: repo advances to B during
    acceptance sampled at A -> BLOCKED, never DELIVERED(A). Deterministic:
    barrier inside fresh-checkout verification, no sleeps."""
    import asyncio as _asyncio

    from test_lifecycle import drive_project, standard_adapters, start_planned_project
    from test_lifecycle import make_orch as _make_lifecycle_orch

    async def main() -> None:
        orch = await _make_lifecycle_orch(tmp_path, standard_adapters())
        pid = await start_planned_project(tmp_path, orch)
        project = await drive_project(orch, pid)
        assert project["state"] == "DELIVERED", project.get("blocking_reason")
        sha_a = project["delivery_sha"]
        target = orch.db.get("projects", orch.db.get("product_projects", pid)["target_project_id"])
        repo = Path(target["path"])
        assert _git(repo, "rev-parse", "HEAD") == sha_a

        # Re-open acceptance, then advance the repo mid-acceptance via a
        # barrier inside fresh-checkout verification.
        orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE", "acceptance_state": "PENDING"})
        real_fresh = orch.coordinator._fresh_checkout_verify
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _barrier_fresh(r, s, p=None, plan=None):  # noqa: ANN001, ANN202
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=120)
            return await real_fresh(r, s, p, plan)

        orch.coordinator._fresh_checkout_verify = _barrier_fresh  # type: ignore[method-assign]
        accept_task = asyncio.create_task(orch.coordinator.run_acceptance(pid))
        await asyncio.wait_for(entered.wait(), timeout=120)
        # External advancement while acceptance is inside fresh checkout.
        (repo / "race.txt").write_text("external commit\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "race B")
        sha_b = _git(repo, "rev-parse", "HEAD")
        assert sha_b != sha_a
        release.set()
        result = await asyncio.wait_for(accept_task, timeout=300)
        assert result["ok"] is False, result
        final = orch.db.get("product_projects", pid)
        assert final["state"] != "DELIVERED", "superseded SHA must never deliver"
        assert final.get("delivery_sha") != sha_b, "no delivery record for the moved candidate"
        assert _git(repo, "rev-parse", "HEAD") == sha_b, "operator repo untouched by the fence"
        reason = (final.get("blocking_reason") or "") + str(result.get("findings"))
        assert "candidate changed" in reason or "changed during acceptance" in reason, reason
        await orch.shutdown()

    _asyncio.run(main())


def test_delivery_race_dirty_workspace_blocks(tmp_path: Path):
    """F-PROV-04 dirty variant: HEAD still A but tree dirtied mid-acceptance."""
    import asyncio as _asyncio

    from test_lifecycle import drive_project, standard_adapters, start_planned_project
    from test_lifecycle import make_orch as _make_lifecycle_orch

    async def main() -> None:
        orch = await _make_lifecycle_orch(tmp_path, standard_adapters())
        pid = await start_planned_project(tmp_path, orch)
        project = await drive_project(orch, pid)
        assert project["state"] == "DELIVERED", project.get("blocking_reason")
        target = orch.db.get("projects", orch.db.get("product_projects", pid)["target_project_id"])
        repo = Path(target["path"])
        orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE", "acceptance_state": "PENDING"})
        real_fresh = orch.coordinator._fresh_checkout_verify
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _barrier_fresh(r, s, p=None, plan=None):  # noqa: ANN001, ANN202
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=120)
            return await real_fresh(r, s, p, plan)

        orch.coordinator._fresh_checkout_verify = _barrier_fresh  # type: ignore[method-assign]
        accept_task = asyncio.create_task(orch.coordinator.run_acceptance(pid))
        await asyncio.wait_for(entered.wait(), timeout=120)
        (repo / "uncommitted.txt").write_text("dirt\n")
        release.set()
        result = await asyncio.wait_for(accept_task, timeout=300)
        assert result["ok"] is False, result
        final = orch.db.get("product_projects", pid)
        assert final["state"] != "DELIVERED"
        assert "dirty" in ((final.get("blocking_reason") or "") + str(result.get("findings"))).lower()
        # P-28: the dirt is still there (fence never auto-commits/discards).
        assert (repo / "uncommitted.txt").read_text() == "dirt\n"
        await orch.shutdown()

    _asyncio.run(main())


def test_reviewer_mutation_is_attributed_not_certified(tmp_path: Path, workspace: Path):
    """P-32/§36: a reviewer that dirties the tree gets a PROVIDER write row
    for its own commit (attributed, never UNKNOWN) — and therefore joins the
    writer set for later ranges instead of silently certifying them."""
    from orchestrator.providers.fake import WorkspaceWriterProvider

    async def main() -> None:
        adapters = {
            "fake-impl": FakeAdapter("fake-impl", ["ok", "ok", "ok"]),
            "fake-rev": WorkspaceWriterProvider("fake-rev", filename="reviewer_note.txt", content="review"),
        }
        cfg = make_config(providers=["fake-impl", "fake-rev"])
        cfg.raw.setdefault("priority", {}).update(
            {
                "planning": ["fake-impl"],
                "implementation": ["fake-impl"],
                "testing": ["fake-impl"],
                "review": ["fake-rev"],
                "repair": ["fake-impl"],
            }
        )
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        db = orch.db
        # Review output is unparseable (writer provider emits no findings
        # block) -> mission UNVERIFIED, but provenance must be exact.
        assert db.get("missions", mission["id"])["status"] == MissionStatus.UNVERIFIED.value
        rev_runs = db.query("SELECT * FROM provider_runs WHERE mission_id=? AND role='review'", (mission["id"],))
        assert rev_runs, "review run must exist"
        rev_rows = db.query("SELECT * FROM write_provenance WHERE run_id=?", (rev_runs[0]["id"],))
        assert len(rev_rows) == 1, "review run gets its own write row"
        assert rev_rows[0]["actor_type"] == "PROVIDER"
        assert rev_rows[0]["provider"] == "fake-rev"
        assert rev_rows[0]["result_sha"] != rev_rows[0]["base_sha"], "reviewer change is a real commit"
        # ...so a later range containing it names the reviewer as a writer.
        from orchestrator.provenance import mission_provider_writers

        writers, complete = mission_provider_writers(db, mission["id"])
        assert "fake-rev" in writers
        assert complete, "attributed reviewer dirt keeps provenance complete (not UNKNOWN)"
        await orch.shutdown()

    asyncio.run(main())


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


def test_final_validation_repair_requires_rereview(tmp_path: Path):
    """P-11/§56: review PASS at A, validation fails, repair creates B,
    revalidation passes -> a fresh review of B is required before completion."""
    import json as _json

    from orchestrator.providers.fake import FindingsProvider, WorkspaceWriterProvider

    async def main() -> None:
        repo = tmp_path / "ws"
        repo.mkdir()
        (repo / "package.json").write_text(_json.dumps({"name": "v", "scripts": {"test": "node check.js"}}))
        (repo / "check.js").write_text(
            "const fs = require('fs');\n"
            "const go = JSON.parse(fs.readFileSync('./ready.json', 'utf8')).go === true;\n"
            "if (!go) { console.error('NOT READY'); process.exit(1); }\n"
            "console.log('READY_OK');\n"
        )
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")
        adapters = {
            "fake-impl": FakeAdapter("fake-impl", ["ok", "ok", "ok"]),
            "fake-rev": FindingsProvider("fake-rev", findings_script=[[], []]),
            "fake-rep": WorkspaceWriterProvider("fake-rep", filename="ready.json", content='{"go":true}'),
        }
        cfg = make_config(providers=["fake-impl", "fake-rev", "fake-rep"])
        cfg.raw.setdefault("priority", {}).update(
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
        _seed_project(orch, repo)
        mission = orch.create_mission("p1", "Gate the release", "make check.js pass", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        db = orch.db
        assert db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        reviews = db.query("SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at ASC", (mission["id"],))
        assert len(reviews) == 2, f"final-validation repair must trigger re-review, got {len(reviews)}"
        assert reviews[0]["reviewed_head_sha"] != reviews[1]["reviewed_head_sha"]
        repairs = db.query("SELECT * FROM provider_runs WHERE mission_id=? AND role='repair'", (mission["id"],))
        assert len(repairs) == 1
        assert (
            reviews[1]["reviewed_head_sha"] == repairs[0]["git_commit_after"]
            or reviews[1]["reviewed_head_sha"] == db.get("missions", mission["id"])["git_head"]
        )
        await orch.shutdown()

    asyncio.run(main())


def test_delivered_project_has_complete_sha_evidence(tmp_path: Path):
    """End-to-end: a DELIVERED product's delivery SHA carries review,
    verification, criterion, and fresh evidence with complete writers."""
    import json as _json

    from test_lifecycle import drive_project, standard_adapters, start_planned_project
    from test_lifecycle import make_orch as _make_lifecycle_orch

    async def main() -> None:
        orch = await _make_lifecycle_orch(tmp_path, standard_adapters())
        pid = await start_planned_project(tmp_path, orch)
        project = await drive_project(orch, pid)
        assert project["state"] == "DELIVERED", project.get("blocking_reason")
        sha = project["delivery_sha"]
        assert sha and len(sha) == 40
        db = orch.db
        # Review attempts bound to phase candidates exist and are independent.
        reviews = db.query(
            "SELECT r.* FROM reviews r JOIN project_phases p ON p.mission_id=r.mission_id WHERE p.project_id=?",
            (pid,),
        )
        assert reviews, "delivery requires recorded review attempts"
        assert all(r["independent"] == 1 for r in reviews), "all delivery reviews must be independent"
        assert all(r["reviewed_head_sha"] for r in reviews)
        # Verification + fresh attempts at exactly the delivery SHA.
        assert db.query(
            "SELECT id FROM verification_attempts WHERE product_project_id=? AND sha=? AND status='passed' LIMIT 1",
            (pid, sha),
        ), "verification must cover the delivery SHA"
        assert db.query(
            "SELECT id FROM fresh_checkout_attempts WHERE project_id=? AND sha=? AND status='passed' LIMIT 1",
            (pid, sha),
        ), "fresh checkout must cover the delivery SHA"
        # Criteria: workdir pass + fresh replay at the delivery SHA.
        criteria = db.query("SELECT criterion_id FROM criterion_results WHERE project_id=?", (pid,))
        assert criteria
        for c in criteria:
            row = db.query(
                "SELECT * FROM criterion_results WHERE project_id=? AND criterion_id=?", (pid, c["criterion_id"])
            )[0]
            if row["status"] == "WAIVED":
                continue
            assert row["status"] == "SATISFIED"
            assert row["sha"] == sha
            fresh_att = db.query(
                "SELECT id FROM criterion_attempts WHERE project_id=? AND criterion_id=?"
                " AND checked_sha=? AND result='SATISFIED' AND context='fresh' LIMIT 1",
                (pid, c["criterion_id"], sha),
            )
            assert fresh_att, f"criterion {c['criterion_id']} must be replayed fresh at {sha[:8]}"
        # Writer provenance complete; delivery report carries it.
        report = (
            _json.loads(project["delivery_report"])
            if isinstance(project["delivery_report"], str)
            else project["delivery_report"]
        )
        assert report["git_sha"] == sha
        assert report["writers"], "delivery report must name writers"
        assert report["verification_sha"] == sha
        assert report["fresh_checkout_sha"] == sha
        assert report["review_attempts"], "delivery report must list review attempts"
        await orch.shutdown()

    asyncio.run(main())
