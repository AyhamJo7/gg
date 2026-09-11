"""Deterministic end-to-end tests for the Idea-to-Product coordinator.

Fake providers drive real missions (real git repos, real toolchain runs, real
review/verify loops) without burning subscription quota.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from orchestrator.db import Database
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter, PlanProvider, default_test_plan, gated_test_plan
from orchestrator.sandbox import sandbox_available

TERMINAL = {"COMPLETED", "FAILED", "UNVERIFIED", "CANCELLED"}


def lifecycle_config(tmp_path: Path, providers: list[str]) -> Any:
    cfg = make_config(providers=providers)
    cfg.raw.setdefault("lifecycle", {})["workspace_root"] = str(tmp_path / "products")
    cfg.raw.setdefault("orchestration", {})["max_provider_wait_seconds"] = 5
    priorities = cfg.raw.setdefault("priority", {})
    priorities["planning"] = ["fake-planner"]
    workers = [p for p in providers if p != "fake-planner"]
    for role in ("implementation", "testing", "review", "repair"):
        priorities[role] = workers
    return cfg


async def make_orch(tmp_path: Path, adapters: dict[str, FakeAdapter], db_name: str = "orch.db") -> Orchestrator:
    db = Database(tmp_path / db_name)
    orch = Orchestrator(db, lifecycle_config(tmp_path, list(adapters.keys())), adapters)
    await orch.registry.detect_all()
    return orch


def seed_toolchain(repo: Path, extra_scripts: dict[str, str] | None = None) -> None:
    # npm script BODIES are opaque to GG's criterion allowlist (only the
    # top-level command, e.g. "npm run check", is checked) — tests use that
    # to drive fixed exit codes / side effects without an inline -e/-c flag
    # on the criterion's own verify command, which is now rejected (F-LIFE-01
    # RCE hardening: only file-based or plain allowlisted commands run).
    scripts = {
        "test": 'node -e "process.exit(0)"',
        "build": 'node -e "process.exit(0)"',
    }
    if extra_scripts:
        scripts.update(extra_scripts)
    (repo / "package.json").write_text(json.dumps({"name": "lifecycle-target", "scripts": scripts}))


async def drive_mission(db: Database, mission_id: str, timeout_s: float = 120.0) -> dict[str, Any]:
    for _ in range(int(timeout_s / 0.1)):
        await asyncio.sleep(0.1)
        row = db.get("missions", mission_id)
        assert row is not None
        if row["status"] in TERMINAL:
            return row
    raise AssertionError(f"mission {mission_id} did not terminate: {db.get('missions', mission_id)}")


async def drive_project(
    orch: Orchestrator, project_id: str, timeout_s: float = 180.0, expect: str = "DELIVERED"
) -> dict[str, Any]:
    """Advance until the project leaves active execution (missions run inline)."""
    db = orch.db
    for _ in range(int(timeout_s / 0.2)):
        project = db.get("product_projects", project_id)
        assert project is not None
        for engine in list(orch._engines.values()):
            engine.wake()  # mimic the scheduler tick (no background loop in tests)
        running = db.query(
            "SELECT m.id FROM project_phases p JOIN missions m ON p.mission_id=m.id "
            "WHERE p.project_id=? AND m.status NOT IN ('COMPLETED','FAILED','UNVERIFIED','CANCELLED')",
            (project_id,),
        )
        await orch.coordinator.advance_project(project_id)
        project = db.get("product_projects", project_id) or {}
        if project.get("state") in ("DELIVERED", "FAILED", "CANCELLED", "BLOCKED", "WAITING_FOR_HUMAN"):
            if not running and project.get("state") in ("DELIVERED", "FAILED", "CANCELLED"):
                return project
            if project.get("state") in ("BLOCKED", "WAITING_FOR_HUMAN") and not running:
                return project
        await asyncio.sleep(0.2)
    raise AssertionError(f"project {project_id} did not settle: {db.get('product_projects', project_id)}")


def standard_adapters(
    plan: dict[str, Any] | None = None, planner_script: list[str] | None = None
) -> dict[str, FakeAdapter]:
    return {
        "fake-planner": PlanProvider("fake-planner", planner_script, plan),
        "fake-a": FakeAdapter("fake-a", ["work"]),
        "fake-b": FakeAdapter("fake-b", ["ok"]),
        "fake-c": FakeAdapter("fake-c", ["ok"]),
    }


async def start_planned_project(
    tmp_path: Path,
    orch: Orchestrator,
    plan: dict[str, Any] | None = None,
    extra_scripts: dict[str, str] | None = None,
    **kwargs: Any,
) -> str:
    project = orch.coordinator.create_project("Test Product", "prove the lifecycle", **kwargs)
    result = await orch.coordinator.generate_plan(project["id"])
    assert result["ok"], result
    orch.coordinator.start_project(project["id"])
    target = orch.db.get("projects", orch.db.get("product_projects", project["id"])["target_project_id"])
    assert target is not None
    seed_toolchain(Path(target["path"]), extra_scripts=extra_scripts)
    # Explicit operator adoption (F-PROV-03 contract): the seeded target
    # content predates any GG run, so the first phase mission blocks at the
    # analyze gate until the operator adopts it. Model that action here.
    await orch.coordinator.advance_project(project["id"])
    for _ in range(100):
        phases = orch.db.query("SELECT mission_id FROM project_phases WHERE project_id=?", (project["id"],))
        mids = [p["mission_id"] for p in phases if p.get("mission_id")]
        if not mids:
            await asyncio.sleep(0.05)
            continue
        mission = orch.db.get("missions", mids[0])
        if mission and mission["status"] == MissionStatus.WAITING_FOR_HUMAN.value:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("phase mission did not reach the attribution gate")
    from orchestrator.provenance import adopt_head_as_human

    adopted = await adopt_head_as_human(orch.db, Path(target["path"]), mission_id=mids[0])
    assert adopted["adopted"] is True, adopted
    gates = orch.db.query("SELECT id FROM human_gates WHERE mission_id=? AND status='open'", (mids[0],))
    assert gates, "attribution gate must be open"
    orch.resolve_gate(gates[0]["id"], "Adopted/committed/cleaned — continue")
    return project["id"]


async def test_idea_to_valid_plan(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    project = orch.coordinator.create_project("Widget", "a local widget tracker", "simple only")
    assert project["state"] == "DRAFT"
    result = await orch.coordinator.generate_plan(project["id"])
    assert result["ok"] is True
    assert result["revision"] == 1
    assert result["plan"]["product_name"] == "Test Product"
    assert len(result["plan"]["phases"]) == 2
    full = orch.coordinator.get_project(project["id"])
    assert full and full["state"] == "PLAN_READY"
    assert full["plan_revision_count"] == 1
    await orch.shutdown()


async def test_malformed_planner_output_bounded_repair(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters(planner_script=["malformed", "ok"]))
    project = orch.coordinator.create_project("Widget", "idea")
    result = await orch.coordinator.generate_plan(project["id"])
    assert result["ok"] is True
    planner = orch.registry.get_adapter("fake-planner")
    assert planner is not None and planner.calls == 2
    await orch.shutdown()


async def test_persistent_malformed_plan_blocks(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters(planner_script=["malformed"]))
    project = orch.coordinator.create_project("Widget", "idea")
    result = await orch.coordinator.generate_plan(project["id"])
    assert result["ok"] is False
    assert orch.db.get("product_projects", project["id"])["state"] == "BLOCKED"
    await orch.shutdown()


async def test_invalid_revision_rejected_and_valid_persisted(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    project = orch.coordinator.create_project("Widget", "idea")
    await orch.coordinator.generate_plan(project["id"])
    bad = default_test_plan()
    bad["phases"][0]["depends_on"] = ["feature"]
    result = orch.coordinator.revise_plan(project["id"], bad, "try cycle")
    assert result["ok"] is False
    full = orch.coordinator.get_project(project["id"])
    assert full and full["plan_revision_count"] == 1
    good = default_test_plan()
    good["product_name"] = "Renamed"
    result = orch.coordinator.revise_plan(project["id"], good, "rename product")
    assert result["ok"] is True and result["revision"] == 2
    full = orch.coordinator.get_project(project["id"])
    assert full and full["plan"]["product_name"] == "Renamed"
    assert full["plan_revision_count"] == 2
    await orch.shutdown()


async def test_full_lifecycle_to_delivered(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    assert project["acceptance_state"] == "DELIVERED"
    assert project["delivery_sha"]
    full = orch.coordinator.get_project(pid)
    assert full is not None
    assert {p["status"] for p in full["phases"]} == {"COMPLETED"}
    assert {e["status"] for e in full["evidence"]} == {"SATISFIED"}
    report = (
        json.loads(full["delivery_report"]) if isinstance(full["delivery_report"], str) else full["delivery_report"]
    )
    assert report["git_sha"] == project["delivery_sha"]
    assert len(report["requirements"]) == 2
    # Exactly one mission per phase, no duplicates.
    missions = orch.db.query("SELECT id FROM missions")
    assert len(missions) == 2
    # No leaked busy providers.
    states = {r["name"]: r["state"] for r in orch.db.query("SELECT name, state FROM providers")}
    assert set(states.values()) <= {"AVAILABLE"}, states
    await orch.shutdown()


async def test_no_duplicate_mission_on_repeated_advance(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    for _ in range(5):
        await orch.coordinator.advance_project(pid)
    missions = orch.db.query("SELECT id FROM missions")
    assert len(missions) == 1
    await drive_project(orch, pid)
    assert len(orch.db.query("SELECT id FROM missions")) == 2
    await orch.shutdown()


async def test_failed_phase_bounded_retry_then_blocked(tmp_path: Path):
    adapters = standard_adapters()
    adapters.update(
        {
            "fake-a": FakeAdapter("fake-a", ["crash"]),
            "fake-b": FakeAdapter("fake-b", ["crash"]),
            "fake-c": FakeAdapter("fake-c", ["crash"]),
        }
    )
    orch = await make_orch(tmp_path, adapters)
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid, expect="BLOCKED")
    assert project["state"] == "BLOCKED"
    assert project["state"] != "DELIVERED"
    phases = {p["phase_key"]: p for p in orch.db.query("SELECT * FROM project_phases WHERE project_id=?", (pid,))}
    assert phases["foundation"]["status"] == "FAILED"
    assert phases["foundation"]["attempts"] == 2  # bounded: max_attempts honored
    assert len(orch.db.query("SELECT id FROM missions")) == 2
    await orch.shutdown()


async def test_gate_blocks_only_dependent_work(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters(gated_test_plan()))
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid, expect="WAITING_FOR_HUMAN")
    assert project["state"] == "WAITING_FOR_HUMAN"
    phases = {p["phase_key"]: p for p in orch.db.query("SELECT * FROM project_phases WHERE project_id=?", (pid,))}
    assert phases["foundation"]["status"] == "COMPLETED"
    assert phases["feature"]["status"] == "WAITING_FOR_HUMAN"
    assert phases["feature"]["mission_id"] is None  # never launched while gated
    gates = orch.db.query("SELECT * FROM project_gates WHERE project_id=? AND status='open'", (pid,))
    assert len(gates) == 1
    assert gates[0]["required_vars"] == '["TEST_TOKEN"]'
    await orch.shutdown()


async def test_gate_resolution_resumes_to_delivered(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters(gated_test_plan()))
    pid = await start_planned_project(tmp_path, orch)
    await drive_project(orch, pid, expect="WAITING_FOR_HUMAN")
    gate = orch.db.query("SELECT * FROM project_gates WHERE project_id=? AND status='open'", (pid,))[0]
    # Missing .env value: resolution refused, nothing exposed.
    refused = await orch.coordinator.resolve_gate(pid, gate["id"], "done")
    assert refused["ok"] is False
    target_id = orch.db.get("product_projects", pid)["target_project_id"]
    repo = Path(orch.db.get("projects", target_id)["path"])
    with (repo / ".env").open("a") as fh:
        fh.write("TEST_TOKEN=fake-token-for-tests\n")
    resolved = await orch.coordinator.resolve_gate(pid, gate["id"], "configured")
    assert resolved["ok"] is True
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    await orch.shutdown()


async def test_secret_values_never_persisted(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters(gated_test_plan()))
    pid = await start_planned_project(tmp_path, orch)
    await drive_project(orch, pid, expect="WAITING_FOR_HUMAN")
    gate = orch.db.query("SELECT * FROM project_gates WHERE project_id=? AND status='open'", (pid,))[0]
    target_id = orch.db.get("product_projects", pid)["target_project_id"]
    repo = Path(orch.db.get("projects", target_id)["path"])
    (repo / ".env").write_text("TEST_TOKEN=super-secret-value-999\n")
    assert (await orch.coordinator.resolve_gate(pid, gate["id"], "x"))["ok"] is True
    blob = json.dumps(orch.db.query("SELECT * FROM project_gates WHERE project_id=?", (pid,)))
    assert "super-secret-value-999" not in blob
    assert "TEST_TOKEN" in blob  # names are fine; values never are
    await orch.shutdown()


async def test_restart_between_phases_no_duplicates(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    # Launch the first phase mission, drive it to COMPLETED, then "restart".
    await orch.coordinator.advance_project(pid)
    first = orch.db.query("SELECT * FROM project_phases WHERE project_id=? AND phase_key='foundation'", (pid,))[0]
    assert first["mission_id"]
    await drive_mission(orch.db, first["mission_id"])
    before = [m["id"] for m in orch.db.query("SELECT id FROM missions ORDER BY created_at")]
    await orch.shutdown()
    # New orchestrator instance on the same database = backend restart.
    orch2 = await make_orch(tmp_path, standard_adapters(), db_name="orch.db")
    await orch2.coordinator.recover()
    project = await drive_project(orch2, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    after = [m["id"] for m in orch2.db.query("SELECT id FROM missions ORDER BY created_at")]
    assert after[: len(before)] == before  # history preserved, no relaunch of phase 1
    assert len(after) == 2  # exactly one mission per phase
    await orch2.shutdown()


async def test_restart_during_execution_recovers(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    await orch.coordinator.advance_project(pid)
    phase = orch.db.query("SELECT * FROM project_phases WHERE project_id=? AND phase_key='foundation'", (pid,))[0]
    assert phase["mission_id"]
    # Simulate backend death: drop in-memory engines without touching the DB.
    orch._engines.clear()
    for task in list(orch._engine_tasks.values()):
        task.cancel()
    orch._engine_tasks.clear()
    await orch.shutdown()
    orch2 = await make_orch(tmp_path, standard_adapters(), db_name="orch.db")
    # Mission recovery relaunches the in-flight engine, coordinator re-drives.
    for row in orch2.db.query(
        "SELECT id FROM missions WHERE status NOT IN ('COMPLETED','FAILED','UNVERIFIED','CANCELLED')"
    ):
        orch2._launch_engine(row["id"])
    project = await drive_project(orch2, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    await orch2.shutdown()


async def test_acceptance_recomputes_from_checks_not_stored_evidence(tmp_path: Path):
    """Stored evidence rows alone prove nothing; verdicts come from executed checks."""
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED"
    # Delete stored requirement evidence: acceptance must recompute from checks.
    orch.db.execute("DELETE FROM requirement_evidence WHERE project_id=?", (pid,))
    orch.db.update(
        "product_projects", pid, {"state": "FINAL_ACCEPTANCE", "acceptance_state": "PENDING", "delivery_sha": None}
    )
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is True, result
    assert orch.db.get("product_projects", pid)["state"] == "DELIVERED"
    # Recorded check rows carry the accepted SHA and exit codes.
    rows = orch.db.query("SELECT * FROM criterion_results WHERE project_id=?", (pid,))
    assert {r["criterion_id"] for r in rows} == {"R1-A1", "R2-A1"}
    assert all(r["status"] == "SATISFIED" and r["exit_code"] == 0 for r in rows)
    assert all(r["sha"] == orch.db.get("product_projects", pid)["delivery_sha"] for r in rows)
    await orch.shutdown()


async def test_acceptance_rejects_failing_toolchain(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED"
    target_id = project["target_project_id"]
    repo = Path(orch.db.get("projects", target_id)["path"])
    pkg = json.loads((repo / "package.json").read_text())
    pkg["scripts"]["test"] = 'node -e "process.exit(3)"'
    (repo / "package.json").write_text(json.dumps(pkg))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)  # noqa: ASYNC221
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "break tests"], check=True)  # noqa: ASYNC221
    orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE", "acceptance_state": "PENDING"})
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is False
    assert orch.db.get("product_projects", pid)["acceptance_state"] == "UNVERIFIED"
    await orch.shutdown()


async def test_acceptance_rejects_open_gate(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters(gated_test_plan()))
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid, expect="WAITING_FOR_HUMAN")
    assert project["state"] == "WAITING_FOR_HUMAN"
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is False
    assert orch.db.get("product_projects", pid)["acceptance_state"] == "EXTERNALLY_BLOCKED"
    await orch.shutdown()


async def test_final_checkpoint_captures_uncommitted_changes(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED"
    target_id = project["target_project_id"]
    repo = Path(orch.db.get("projects", target_id)["path"])
    (repo / "notes.txt").write_text("uncommitted acceptance note\n")
    orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE", "acceptance_state": "PENDING"})
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is True, result
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()  # noqa: ASYNC221
    assert result["sha"] == head
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout  # noqa: ASYNC221
    assert status.strip() == ""
    await orch.shutdown()


async def test_fresh_checkout_installs_and_reproduces(tmp_path: Path):
    """Fresh-checkout acceptance installs dependencies before verifying.

    The test script requires a file: dependency that exists only after
    install, proving install+verify ran inside the clone (not the source).
    """
    import subprocess as _subprocess  # noqa: ASYNC221 - test scaffolding, sync context ok

    orch = await make_orch(tmp_path, standard_adapters())
    repo = tmp_path / "freshrepo"
    repo.mkdir()
    vendor = repo / "vendor" / "mydep"
    vendor.mkdir(parents=True)
    (vendor / "package.json").write_text(json.dumps({"name": "mydep", "version": "1.0.0", "main": "index.js"}))
    (vendor / "index.js").write_text("module.exports = () => 'vendored';\n")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "fresh",
                "dependencies": {"mydep": "file:./vendor/mydep"},
                "scripts": {
                    "test": "node -e \"console.log('CWD:'+process.cwd());process.exit(require('mydep')()==='vendored'?0:1)\""
                },
            }
        )
    )
    _subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: ASYNC221
    _subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)  # noqa: ASYNC221
    _subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)  # noqa: ASYNC221
    sha = _subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    # Sanity: without install the check genuinely fails.
    assert (repo / "node_modules").exists() is False
    ok, detail = await orch.coordinator._fresh_checkout_verify(repo, sha)
    assert ok is True, detail
    assert "npm install ok" in detail
    await orch.shutdown()


@pytest.mark.skipif(not sandbox_available(), reason="bubblewrap (bwrap) not installed")
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
async def test_fresh_checkout_install_escape_is_contained(tmp_path: Path):
    """A malicious PEP 517 build backend in the accepted SHA's own pyproject.toml
    cannot escape the sandbox during _fresh_checkout_verify's install step.

    Regression for the round-9 finding: `_install` used to call run_process
    directly (full ambient env, no sandbox) — a build backend hook could read/
    write anything the real orchestrator process could. Proven exploitable
    pre-fix by manually reproducing this exact scenario outside the sandbox:
    the hook wrote a real marker file into the real $HOME. This test proves
    the now-sandboxed _install path contains that same attempt.
    """
    import subprocess as _subprocess  # noqa: ASYNC221 - test scaffolding, sync context ok

    orch = await make_orch(tmp_path, standard_adapters())
    repo = tmp_path / "freshrepo"
    repo.mkdir()

    pkg = repo / "evil_backend_pkg"
    pkg.mkdir()
    (pkg / "evil_backend.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def build_editable(wheel_directory, config_settings=None, metadata_directory=None):\n"
        "    marker = Path(os.environ.get('HOME', '/')) / 'MARKER_ESCAPED_HOME'\n"
        "    try:\n"
        "        marker.write_text('pwned-outside-home')\n"
        "    except Exception:\n"
        "        pass\n"
        "    Path('BUILD_HOOK_RAN.txt').write_text('yes')\n"
        "    raise SystemExit('intentional-stop-after-proof')\n"
        "def get_requires_for_build_editable(config_settings=None):\n"
        "    return []\n"
    )
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "evil-pkg"\nversion = "0.1.0"\n\n'
        '[build-system]\nrequires = []\nbuild-backend = "evil_backend"\nbackend-path = ["."]\n'
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "fresh"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
        'dependencies = ["evil-pkg"]\n\n'
        '[tool.uv.sources]\nevil-pkg = { path = "evil_backend_pkg", editable = true }\n'
    )
    _subprocess.run(["uv", "lock"], cwd=repo, check=True, capture_output=True)  # noqa: ASYNC221
    _subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: ASYNC221
    _subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)  # noqa: ASYNC221
    _subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)  # noqa: ASYNC221
    sha = _subprocess.run(  # noqa: ASYNC221
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    real_home_marker = Path.home() / "MARKER_ESCAPED_HOME"
    assert not real_home_marker.exists(), "pre-existing marker from a prior failed run — remove before re-testing"
    try:
        ok, detail = await orch.coordinator._fresh_checkout_verify(repo, sha)
        # The build backend deliberately fails (SystemExit), so install fails —
        # what matters is that its escape attempt landed nowhere real.
        assert ok is False, detail
        assert not real_home_marker.exists(), "build backend escaped the sandbox and wrote into the real $HOME"
    finally:
        real_home_marker.unlink(missing_ok=True)
        await orch.shutdown()


async def test_cancel_and_retry_phase(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    await orch.coordinator.advance_project(pid)
    await orch.coordinator.cancel_project(pid)
    project = orch.db.get("product_projects", pid)
    assert project["state"] == "CANCELLED"
    # Terminal states are immutable.
    await orch.coordinator.cancel_project(pid)
    assert orch.db.get("product_projects", pid)["state"] == "CANCELLED"
    with pytest.raises(ValueError):
        await orch.coordinator.generate_plan(pid)
    await orch.shutdown()


async def test_quota_failover_bounded(tmp_path: Path):
    adapters = standard_adapters()
    adapters["fake-a"] = FakeAdapter("fake-a", ["ratelimit", "work"])
    orch = await make_orch(tmp_path, adapters)
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    await orch.shutdown()


async def test_revision_cannot_drop_completed_phase(tmp_path: Path):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED"
    # Terminal project: revision refused.
    with pytest.raises(ValueError):
        orch.coordinator.revise_plan(pid, default_test_plan(), "too late")
    await orch.shutdown()
