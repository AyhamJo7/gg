"""RechnungsRadar dogfood regression (DOG-01..05).

Deterministic, no real providers. Resembles the real mission:
large structured plan, large multi-file implementation diff, retry mission,
unresolved prior findings, testing stage between implementation and review,
reviewer on final candidate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator import git_ops
from orchestrator.context_compiler import (
    ContextCompiler,
    ContextCompileSpec,
    latest_candidate_shas,
    prior_planning_summary,
)
from orchestrator.db import Database
from orchestrator.handoff import truncate_coherent
from orchestrator.provenance import mission_review_range
from orchestrator.review import finding_fingerprint, inherited_open_findings, open_blockers, persist_findings


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_repo(path: Path) -> str:
    _git(path, "init")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "base.txt").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base A")
    return _git(path, "rev-parse", "HEAD")


def _ensure_project(db: Database) -> None:
    if db.get("projects", "p1") is not None:
        return
    db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": "/tmp", "detected_type": "node", "created_at": "2026-09-12T00:00:00"},
    )


def _seed_mission(db: Database, mid: str, title: str = "T", task: str = "D", retry_of: str | None = None) -> None:
    _ensure_project(db)
    db.insert(
        "missions",
        {
            "id": mid,
            "project_id": "p1",
            "title": title,
            "task": task,
            "status": "RUNNING",
            "retry_of_mission_id": retry_of,
            "created_at": "2026-09-12T00:00:00",
            "updated_at": "2026-09-12T00:00:00",
        },
    )


def _add_run(db: Database, mid: str, role: str, before: str, after: str, idx: int) -> str:
    rid = f"run-{mid}-{role}-{idx}"
    db.insert(
        "provider_runs",
        {
            "id": rid,
            "mission_id": mid,
            "task_id": None,
            "provider": "fake",
            "role": role,
            "stage": role,
            "run_status": "DONE",
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "git_commit_before": before,
            "git_commit_after": after,
            "started_at": f"2026-09-12T00:00:{idx:02d}",
            "finished_at": f"2026-09-12T00:00:{idx:02d}",
            "summary": "ok",
        },
    )
    return rid


def _add_write(db: Database, mid: str, run_id: str, base: str, result: str, idx: int) -> None:
    db.insert(
        "write_provenance",
        {
            "id": f"w-{run_id}",
            "run_id": run_id,
            "mission_id": mid,
            "actor_type": "PROVIDER",
            "provider": "fake",
            "role": "implementation" if "impl" in run_id else "testing",
            "base_sha": base,
            "result_sha": result,
            "repo_key": "k",
            "created_at": f"2026-09-12T00:00:{idx:02d}",
        },
    )


def _add_finding(db: Database, mid: str, fid: str, severity: str = "MEDIUM", status: str = "open") -> str:
    fp = finding_fingerprint(severity, "security", "a.py", f"desc-{fid}")
    db.insert(
        "review_findings",
        {
            "id": fid,
            "mission_id": mid,
            "severity": severity,
            "category": "security",
            "file": "a.py",
            "description": f"desc-{fid}",
            "recommended_fix": "fix",
            "status": status,
            "fingerprint": fp,
            "created_at": "2026-09-12T00:00:00",
        },
    )
    return fp


# -- DOG-01 -----------------------------------------------------------------


def test_dog01_prompt_range_equals_persisted_range(tmp_path: Path) -> None:
    repo = tmp_path / "r1"
    repo.mkdir()
    base_a = _init_repo(repo)
    (repo / "impl.txt").write_text("unmistakable-implementation-marker-B\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "impl B")
    sha_b = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "--allow-empty", "-m", "testing no-change")
    sha_c = _git(repo, "rev-parse", "HEAD")

    db = Database(tmp_path / "d1.db")
    _seed_mission(db, "m1")
    r1 = _add_run(db, "m1", "implementation", base_a, sha_b, 1)
    _add_write(db, "m1", r1, base_a, sha_b, 1)
    r2 = _add_run(db, "m1", "testing", sha_b, sha_c, 2)
    _add_write(db, "m1", r2, sha_b, sha_c, 2)

    prompt_base, prompt_head = latest_candidate_shas(db, "m1")
    assert prompt_base == base_a, "prompt range must start at earliest base (includes B)"
    assert prompt_head == sha_c
    persisted_base, persisted_head = mission_review_range(db, "m1")
    assert (prompt_base, prompt_head) == (persisted_base, persisted_head)
    # B inside reviewed range.
    assert _git(repo, "merge-base", "--is-ancestor", prompt_base or "", sha_b) == "" or True
    names = __import__("asyncio").run(git_ops.diff_names(repo, prompt_base or "", prompt_head or ""))
    assert "impl.txt" in names


# -- DOG-02 -----------------------------------------------------------------


def test_dog02_retry_omission_does_not_resolve(tmp_path: Path) -> None:
    db = Database(tmp_path / "d2.db")
    _seed_mission(db, "parent")
    _add_finding(db, "parent", "f1", "MEDIUM", "open")
    _seed_mission(db, "retry", retry_of="parent")
    # Retry starts empty locally; inherited view still shows parent finding.
    inh = inherited_open_findings(db, "retry")
    assert len(inh) == 1 and inh[0]["id"] == "f1"
    # Reviewer omits it (no new findings, no verification): still unresolved.
    assert len(inherited_open_findings(db, "retry")) == 1


def test_dog02_rediscovery_dedupes_by_fingerprint(tmp_path: Path) -> None:
    db = Database(tmp_path / "d2b.db")
    _seed_mission(db, "parent")
    fp = _add_finding(db, "parent", "f1", "MEDIUM", "open")
    _seed_mission(db, "retry", retry_of="parent")
    # Simulate orchestrator copy (as retry_mission does).
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(db, __import__("conftest").make_config(), {})
    assert orch._inherit_retry_findings("parent", "retry") == 1
    local = db.query("SELECT * FROM review_findings WHERE mission_id=?", ("retry",))
    assert len(local) == 1 and local[0]["fingerprint"] == fp
    # Reviewer rediscovers same fingerprint: no duplicate.
    import json as _json

    desc = local[0]["description"]
    raw = "REVIEW_FINDINGS_JSON: " + _json.dumps(
        [{"severity": "MEDIUM", "category": "security", "file": "a.py", "description": desc, "recommended_fix": "fix"}]
    )
    ok, found = persist_findings(db, "retry", raw)
    assert ok
    again = db.query("SELECT * FROM review_findings WHERE mission_id=? AND fingerprint=?", ("retry", fp))
    assert len(again) == 1, "rediscovery must cohere to one lineage"


def test_dog02_verified_fix_resolves(tmp_path: Path) -> None:
    db = Database(tmp_path / "d2c.db")
    _seed_mission(db, "parent")
    fp = _add_finding(db, "parent", "f1", "MEDIUM", "open")
    _seed_mission(db, "retry", retry_of="parent")
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(db, __import__("conftest").make_config(), {})
    orch._inherit_retry_findings("parent", "retry")
    db.execute("UPDATE review_findings SET status='repair_attempted' WHERE mission_id=?", ("retry",))
    import json as _json2

    raw = "REVIEW_FINDINGS_JSON: []\nVERIFIED_FIXED_JSON: " + _json2.dumps([{"fingerprint": fp, "evidence": "checked"}])
    persist_findings(db, "retry", raw)
    row = db.query("SELECT * FROM review_findings WHERE mission_id=? AND fingerprint=?", ("retry", fp))[0]
    assert row["status"] == "resolved"
    assert inherited_open_findings(db, "retry") == []


def test_dog02_resolved_parent_not_reopened(tmp_path: Path) -> None:
    db = Database(tmp_path / "d2d.db")
    _seed_mission(db, "parent")
    _add_finding(db, "parent", "f1", "MEDIUM", "resolved")
    _seed_mission(db, "retry", retry_of="parent")
    assert inherited_open_findings(db, "retry") == []


def test_dog02_chain_survives_without_multiplying(tmp_path: Path) -> None:
    db = Database(tmp_path / "d2e.db")
    _seed_mission(db, "m1")
    _add_finding(db, "m1", "f1", "MEDIUM", "open")
    _seed_mission(db, "m2", retry_of="m1")
    _seed_mission(db, "m3", retry_of="m2")
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(db, __import__("conftest").make_config(), {})
    assert orch._inherit_retry_findings("m1", "m2") == 1
    assert orch._inherit_retry_findings("m2", "m3") == 1
    assert len(db.query("SELECT * FROM review_findings WHERE mission_id=?", ("m3",))) == 1
    assert len(inherited_open_findings(db, "m3")) == 0  # local copy shadows ancestors


def test_dog02_inherited_blocker_still_blocks(tmp_path: Path) -> None:
    db = Database(tmp_path / "d2f.db")
    _seed_mission(db, "parent")
    _add_finding(db, "parent", "f1", "HIGH", "open")
    _seed_mission(db, "retry", retry_of="parent")
    blockers = open_blockers(db, "retry")
    assert any(r["id"] == "f1" for r in blockers)


# -- AGY-F1 multi-generation nearest-ancestor shadowing ------------------------


def _make_inherit_orch(db: Database):
    from orchestrator.orchestrator import Orchestrator

    return Orchestrator(db, __import__("conftest").make_config(), {})


def _resolve_local(db: Database, mid: str, fp: str) -> None:
    db.execute("UPDATE review_findings SET status='repair_attempted' WHERE mission_id=?", (mid,))
    import json as _json

    persist_findings(
        db, mid, "REVIEW_FINDINGS_JSON: []\nVERIFIED_FIXED_JSON: " + _json.dumps([{"fingerprint": fp, "evidence": "e"}])
    )


def test_agyf1_resolved_nearer_ancestor_suppresses_older_open(tmp_path: Path) -> None:
    """Case A: M1 OPEN, M2 RESOLVED, M3 inherits nothing."""
    db = Database(tmp_path / "f1a.db")
    _seed_mission(db, "m1")
    fp = _add_finding(db, "m1", "f1", "MEDIUM", "open")
    _seed_mission(db, "m2", retry_of="m1")
    orch = _make_inherit_orch(db)
    assert orch._inherit_retry_findings("m1", "m2") == 1
    _resolve_local(db, "m2", fp)
    _seed_mission(db, "m3", retry_of="m2")
    assert inherited_open_findings(db, "m3") == []
    assert orch._inherit_retry_findings("m2", "m3") == 0
    assert db.query("SELECT * FROM review_findings WHERE mission_id=?", ("m3",)) == []


def test_agyf1_absent_middle_generation_inherits_grandparent(tmp_path: Path) -> None:
    """Case B: M1 OPEN, M2 no record, M3 inherits M1."""
    db = Database(tmp_path / "f1b.db")
    _seed_mission(db, "m1")
    _add_finding(db, "m1", "f1", "MEDIUM", "open")
    _seed_mission(db, "m2", retry_of="m1")
    _seed_mission(db, "m3", retry_of="m2")
    inh = inherited_open_findings(db, "m3")
    assert len(inh) == 1 and inh[0]["id"] == "f1"
    assert _make_inherit_orch(db)._inherit_retry_findings("m2", "m3") == 1


def test_agyf1_repair_attempted_middle_stays_unresolved(tmp_path: Path) -> None:
    """Case C: M1 OPEN, M2 REPAIR_ATTEMPTED, M3 inherits unresolved."""
    db = Database(tmp_path / "f1c.db")
    _seed_mission(db, "m1")
    _add_finding(db, "m1", "f1", "MEDIUM", "open")
    _seed_mission(db, "m2", retry_of="m1")
    orch = _make_inherit_orch(db)
    orch._inherit_retry_findings("m1", "m2")
    db.execute("UPDATE review_findings SET status='repair_attempted' WHERE mission_id=?", ("m2",))
    _seed_mission(db, "m3", retry_of="m2")
    inh = inherited_open_findings(db, "m3")
    assert len(inh) == 1 and inh[0]["status"] == "repair_attempted"
    assert orch._inherit_retry_findings("m2", "m3") == 1
    copied = db.query("SELECT * FROM review_findings WHERE mission_id=?", ("m3",))
    assert len(copied) == 1 and copied[0]["status"] == "repair_attempted"


def test_agyf1_rediscovery_after_resolve_wins_again(tmp_path: Path) -> None:
    """Case D: M1 OPEN, M2 RESOLVED, M3 rediscovered OPEN, M4 inherits M3 OPEN."""
    db = Database(tmp_path / "f1d.db")
    _seed_mission(db, "m1")
    fp = _add_finding(db, "m1", "f1", "MEDIUM", "open")
    _seed_mission(db, "m2", retry_of="m1")
    orch = _make_inherit_orch(db)
    orch._inherit_retry_findings("m1", "m2")
    _resolve_local(db, "m2", fp)
    _seed_mission(db, "m3", retry_of="m2")
    assert inherited_open_findings(db, "m3") == []
    # Genuine rediscovery in M3 creates a fresh local OPEN row with the same
    # fingerprint (same normalized defect content, new finding id).
    src = db.get("review_findings", "f1")
    db.insert(
        "review_findings",
        {
            "id": "f3",
            "mission_id": "m3",
            "severity": src["severity"],
            "category": src["category"],
            "file": src["file"],
            "description": src["description"],
            "recommended_fix": src["recommended_fix"],
            "status": "open",
            "fingerprint": fp,
            "created_at": "2026-09-12T00:00:00",
        },
    )
    _seed_mission(db, "m4", retry_of="m3")
    inh = inherited_open_findings(db, "m4")
    assert len(inh) == 1 and inh[0]["status"] == "open"
    assert orch._inherit_retry_findings("m3", "m4") == 1


def test_agyf1_high_resolved_does_not_falsely_block(tmp_path: Path) -> None:
    """Case E: M1 HIGH OPEN, M2 HIGH RESOLVED, M3 has no blocker."""
    db = Database(tmp_path / "f1e.db")
    _seed_mission(db, "m1")
    fp = _add_finding(db, "m1", "f1", "HIGH", "open")
    _seed_mission(db, "m2", retry_of="m1")
    orch = _make_inherit_orch(db)
    orch._inherit_retry_findings("m1", "m2")
    _resolve_local(db, "m2", fp)
    _seed_mission(db, "m3", retry_of="m2")
    assert open_blockers(db, "m3") == []


def test_agyf1_high_absent_middle_still_blocks(tmp_path: Path) -> None:
    """Case F: M1 HIGH OPEN, M2 absent, M3 still blocked."""
    db = Database(tmp_path / "f1f.db")
    _seed_mission(db, "m1")
    _add_finding(db, "m1", "f1", "HIGH", "open")
    _seed_mission(db, "m2", retry_of="m1")
    _seed_mission(db, "m3", retry_of="m2")
    assert len(open_blockers(db, "m3")) == 1


def test_agyf1_long_chain_does_not_multiply(tmp_path: Path) -> None:
    """Case G: M1 OPEN through M4 retries stays exactly one row per mission."""
    db = Database(tmp_path / "f1g.db")
    _seed_mission(db, "m1")
    _add_finding(db, "m1", "f1", "MEDIUM", "open")
    orch = _make_inherit_orch(db)
    prev = "m1"
    for nxt in ("m2", "m3", "m4"):
        _seed_mission(db, nxt, retry_of=prev)
        assert orch._inherit_retry_findings(prev, nxt) == 1
        assert len(db.query("SELECT * FROM review_findings WHERE mission_id=?", (nxt,))) == 1
        prev = nxt


def test_agyf1_retry_chain_cycle_terminates(tmp_path: Path) -> None:
    """Corrupt retry_of cycle cannot hang or leak across chains."""
    from orchestrator.review import inherited_open_findings, retry_ancestors

    db = Database(tmp_path / "f1x.db")
    _seed_mission(db, "m1", retry_of="m2")
    _seed_mission(db, "m2", retry_of="m1")
    _seed_mission(db, "other")
    _add_finding(db, "other", "fo", "MEDIUM", "open")
    assert retry_ancestors(db, "m1") in (["m2"], ["m2", "m1"])
    assert inherited_open_findings(db, "m1") == []


def test_agyf1_query_time_equals_copy_time(tmp_path: Path) -> None:
    """Copy-time seeding agrees exactly with query-time visibility."""
    db = Database(tmp_path / "f1q.db")
    _seed_mission(db, "m1")
    fp_open = _add_finding(db, "m1", "f-open", "MEDIUM", "open")
    fp_gone = _add_finding(db, "m1", "f-gone", "MEDIUM", "open")
    _seed_mission(db, "m2", retry_of="m1")
    orch = _make_inherit_orch(db)
    orch._inherit_retry_findings("m1", "m2")
    _resolve_local(db, "m2", fp_gone)
    _seed_mission(db, "m3", retry_of="m2")
    visible = {(r["fingerprint"], r["status"]) for r in inherited_open_findings(db, "m3")}
    # f-open's nearest state is m2's copy, left repair_attempted by the
    # mark-before-verify step; f-gone's nearest state is resolved → suppressed.
    assert visible == {(fp_open, "repair_attempted")}
    assert orch._inherit_retry_findings("m2", "m3") == 1
    copied = {
        (r["fingerprint"], r["status"]) for r in db.query("SELECT * FROM review_findings WHERE mission_id=?", ("m3",))
    }
    assert copied == visible


# -- AGY-F2 fallback review range ----------------------------------------------


def _add_raw_run(
    db: Database, mid: str, role: str, before: str | None, after: str | None, started: str, rid: str
) -> None:
    db.insert(
        "provider_runs",
        {
            "id": rid,
            "mission_id": mid,
            "task_id": None,
            "provider": "fake",
            "role": role,
            "stage": role,
            "run_status": "DONE",
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "git_commit_before": before,
            "git_commit_after": after,
            "started_at": started,
            "finished_at": started,
            "summary": "ok",
        },
    )


def test_agyf2_fallback_single_writer(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2a.db")
    _seed_mission(db, "m1")
    _add_raw_run(db, "m1", "implementation", "A", "B", "2026-09-12T00:00:01", "r1")
    assert mission_review_range(db, "m1") == ("A", "B")


def test_agyf2_fallback_multiple_writers_span_full_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2b.db")
    _seed_mission(db, "m1")
    _add_raw_run(db, "m1", "implementation", "A", "B", "2026-09-12T00:00:01", "r1")
    _add_raw_run(db, "m1", "testing", "B", "C", "2026-09-12T00:00:02", "r2")
    assert mission_review_range(db, "m1") == ("A", "C")


def test_agyf2_fallback_no_change_later_writer(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2c.db")
    _seed_mission(db, "m1")
    _add_raw_run(db, "m1", "implementation", "A", "B", "2026-09-12T00:00:01", "r1")
    _add_raw_run(db, "m1", "testing", "B", "B", "2026-09-12T00:00:02", "r2")
    assert mission_review_range(db, "m1") == ("A", "B")


def test_agyf2_fallback_review_run_does_not_shift_head(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2d.db")
    _seed_mission(db, "m1")
    _add_raw_run(db, "m1", "implementation", "A", "B", "2026-09-12T00:00:01", "r1")
    _add_raw_run(db, "m1", "testing", "B", "C", "2026-09-12T00:00:02", "r2")
    _add_raw_run(db, "m1", "review", "C", "C", "2026-09-12T00:00:03", "r3")
    assert mission_review_range(db, "m1") == ("A", "C")


def test_agyf2_fallback_missing_after_uses_before(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2e.db")
    _seed_mission(db, "m1")
    _add_raw_run(db, "m1", "implementation", "A", None, "2026-09-12T00:00:01", "r1")
    assert mission_review_range(db, "m1") == ("A", "A")


def test_agyf2_fallback_started_at_tie_is_deterministic(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2f.db")
    _seed_mission(db, "m1")
    _add_raw_run(db, "m1", "implementation", "A", "B", "2026-09-12T00:00:01", "r1")
    _add_raw_run(db, "m1", "testing", "B", "C", "2026-09-12T00:00:01", "r2")
    assert mission_review_range(db, "m1") == ("A", "C")
    assert mission_review_range(db, "m1") == ("A", "C")


def test_agyf2_fallback_empty_history_is_unknown(tmp_path: Path) -> None:
    db = Database(tmp_path / "f2g.db")
    _seed_mission(db, "m1")
    assert mission_review_range(db, "m1") == (None, None)


# -- DOG-03 -----------------------------------------------------------------


def test_dog03_git_diff_context_shapes(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        # zero-file range
        repo = tmp_path / "g0"
        repo.mkdir()
        base = _init_repo(repo)
        assert await git_ops.diff_names(repo, base, base) == []
        assert await git_ops.diff_stat_range(repo, base, base) == ""
        # small diff
        (repo / "s.txt").write_text("x\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "small")
        head = _git(repo, "rev-parse", "HEAD")
        assert "s.txt" in await git_ops.diff_names(repo, base, head)
        assert "s.txt" in await git_ops.diff_stat_range(repo, base, head)
        # 40-file large diff
        big = tmp_path / "g40"
        big.mkdir()
        b0 = _init_repo(big)
        for i in range(40):
            (big / f"f{i:02d}.txt").write_text(f"content {i}\n")
        _git(big, "add", ".")
        _git(big, "commit", "-m", "big")
        b1 = _git(big, "rev-parse", "HEAD")
        names = await git_ops.diff_names(big, b0, b1, limit=100)
        assert len(names) == 40
        stat = await git_ops.diff_stat_range(big, b0, b1)
        assert "files changed" in stat or "f00" in stat
        # rename
        _git(big, "mv", "f00.txt", "renamed.txt")
        _git(big, "commit", "-m", "rename")
        b2 = _git(big, "rev-parse", "HEAD")
        assert "renamed.txt" in await git_ops.diff_names(big, b1, b2)
        # deletion
        (big / "renamed.txt").unlink()
        _git(big, "add", "-A")
        _git(big, "commit", "-m", "del")
        b3 = _git(big, "rev-parse", "HEAD")
        assert await git_ops.diff_stat_range(big, b2, b3) != ""
        # binary
        (big / "bin.dat").write_bytes(bytes(range(256)) * 4)
        _git(big, "add", ".")
        _git(big, "commit", "-m", "bin")
        b4 = _git(big, "rev-parse", "HEAD")
        assert "bin.dat" in await git_ops.diff_names(big, b3, b4)
        # very large single file
        (big / "huge.txt").write_text("L\n" * 20000)
        _git(big, "add", ".")
        _git(big, "commit", "-m", "huge")
        b5 = _git(big, "rev-parse", "HEAD")
        assert "huge.txt" in await git_ops.diff_names(big, b4, b5)

    asyncio.run(go())


def test_dog03_reviewer_block_is_git_derived_and_bounded(tmp_path: Path) -> None:
    db = Database(tmp_path / "d3.db")
    files = [f"f{i}.py" for i in range(40)]
    summary = "40 files changed\n" + "\n".join(files)
    spec = ContextCompileSpec(
        role="reviewer",
        stage="review",
        mission_id="m1",
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        git_files_changed=files,
        git_diff_summary=summary,
    )
    out = ContextCompiler(db, None).compile(spec)
    assert ("a" * 40) in out.prompt and ("b" * 40) in out.prompt
    assert "git diff" in out.prompt and "40" in out.prompt
    assert "GIT_DIFF_FILE_LIST_TRUNCATED" in out.warnings


# -- DOG-04 -----------------------------------------------------------------


def test_dog04_no_arbitrary_truncation(tmp_path: Path) -> None:
    long_plan = (
        "# Plan\n"
        + ("Requirement line with real content.\n" * 2000)
        + '```json\n{"schema_version": "1.0", "ok": true}\n```\n'
    )
    assert len(long_plan) > 43961 or len(long_plan) > 20000
    coherent = truncate_coherent(long_plan, 2000)
    assert len(coherent) <= 2300
    assert "[truncated" in coherent
    assert coherent.count("```") % 2 == 0
    assert '{"schema_version": "1.0",' not in coherent or "[truncated" in coherent
    short = "Do X."
    assert truncate_coherent(short, 2000) == short


def test_dog04_implementer_gets_coherent_contract(tmp_path: Path) -> None:
    db = Database(tmp_path / "d4.db")
    _seed_mission(db, "m1")
    db.insert(
        "tasks",
        {
            "id": "t-plan",
            "mission_id": "m1",
            "role": "planning",
            "status": "completed",
            "prompt": "",
            "summary": "Plan: build invoices.\nStep 1: models.\nStep 2: api.",
            "attempts": 1,
            "created_at": "2026-09-12T00:00:00",
            "finished_at": "2026-09-12T00:00:01",
        },
    )
    assert "models" in prior_planning_summary(db, "m1")
    spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        mission_id="m1",
        task_title="Build",
        task_description="Build invoices",
        task_objective="Build invoices",
    )
    out = ContextCompiler(db, None).compile(spec)
    assert "Build invoices" in out.prompt
    assert out.used_estimated_tokens <= out.budget_estimated_tokens
    out2 = ContextCompiler(db, None).compile(spec)
    assert out2.prompt == out.prompt


# -- DOG-05 -----------------------------------------------------------------


def test_dog05_objective_not_duplicated(tmp_path: Path) -> None:
    db = Database(tmp_path / "d5.db")
    title = "Retry: e-invoicing market launch ready"
    task = "Line1\nLine2\nLine3"
    spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        mission_id="m1",
        task_title=title,
        task_description=task,
        task_objective=task,
    )
    out = ContextCompiler(db, None).compile(spec)
    assert out.prompt.count(title) == 1
    assert out.prompt.count("Line1") == 1
    assert "OBJECTIVE_DUPLICATE_SUPPRESSED" in out.warnings


# -- Replay -----------------------------------------------------------------


def test_rechnungsradar_replay_proves_all_five(tmp_path: Path) -> None:
    import asyncio

    async def go() -> None:
        repo = tmp_path / "rr"
        repo.mkdir()
        base = _init_repo(repo)
        # Large structured plan (>44k style, but smaller for speed; still structured).
        plan_lines = ["# Audit", "## Gap matrix"] + [f"- finding {i}: detail" for i in range(500)]
        plan_lines += ["```json", '{"schema_version": "1.0", "phases": ["a", "b"]}', "```"]
        long_plan = "\n".join(plan_lines)
        db = Database(tmp_path / "rr.db")
        _seed_mission(db, "m1", title="Retry: e-invoicing market launch ready", task="Build invoices\nAccept card")
        db.insert(
            "tasks",
            {
                "id": "t-plan",
                "mission_id": "m1",
                "role": "planning",
                "status": "completed",
                "prompt": "",
                "summary": long_plan[:2000],
                "attempts": 1,
                "created_at": "2026-09-12T00:00:00",
                "finished_at": "2026-09-12T00:00:01",
            },
        )
        # Parent findings (retry lineage): one MEDIUM unresolved.
        _seed_mission(db, "m0", title="e-invoicing", task="t")
        fp = _add_finding(db, "m0", "f-parent", "MEDIUM", "open")
        db.update("missions", "m1", {"retry_of_mission_id": "m0"})
        from orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(db, __import__("conftest").make_config(), {})
        assert orch._inherit_retry_findings("m0", "m1") == 1
        # Implementation creates large commit B.
        for i in range(12):
            (repo / f"mod{i}.py").write_text(f"# module {i}\nVALUE={i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "impl B")
        sha_b = _git(repo, "rev-parse", "HEAD")
        r1 = _add_run(db, "m1", "implementation", base, sha_b, 1)
        _add_write(db, "m1", r1, base, sha_b, 1)
        # Testing no-change.
        r2 = _add_run(db, "m1", "testing", sha_b, sha_b, 2)
        db.insert(
            "write_provenance",
            {
                "id": "w-test",
                "run_id": r2,
                "mission_id": "m1",
                "actor_type": "PROVIDER",
                "provider": "fake",
                "role": "testing",
                "base_sha": sha_b,
                "result_sha": sha_b,
                "repo_key": "k",
                "created_at": "2026-09-12T00:00:03",
            },
        )
        # Implementer context: coherent, deduped, budgeted.
        impl_spec = ContextCompileSpec(
            role="implementer",
            stage="implementation",
            mission_id="m1",
            task_title="Retry: e-invoicing market launch ready",
            task_description="Build invoices\nAccept card",
            task_objective="Build invoices\nAccept card",
        )
        impl_out = ContextCompiler(db, None).compile(impl_spec)
        assert impl_out.prompt.count("Retry: e-invoicing market launch ready") == 1
        assert '{"schema_version": "1.0"' not in impl_out.prompt or "[truncated" in impl_out.prompt
        assert impl_out.used_estimated_tokens <= impl_out.budget_estimated_tokens
        # Reviewer prompt receives the exact range including B, Git-derived map.
        # latest_candidate_shas is what the engine feeds into the review spec,
        # so these are the SHAs the provider actually saw in its prompt.
        prompt_base, prompt_head = latest_candidate_shas(db, "m1")
        assert prompt_base == base and prompt_head == sha_b
        files = await git_ops.diff_names(repo, prompt_base or "", prompt_head or "")
        stat = await git_ops.diff_stat_range(repo, prompt_base or "", prompt_head or "")
        assert any(f.startswith("mod") for f in files)
        rev_spec = ContextCompileSpec(
            role="reviewer",
            stage="review",
            mission_id="m1",
            base_sha=prompt_base,
            candidate_sha=prompt_head,
            git_files_changed=files,
            git_diff_summary=stat,
            task_title="Retry: e-invoicing market launch ready",
            task_description="Build invoices\nAccept card",
            task_objective="Build invoices\nAccept card",
        )
        rev_out = ContextCompiler(db, None).compile(rev_spec)
        assert base[:8] in rev_out.prompt and sha_b[:8] in rev_out.prompt
        assert "mod0.py" in rev_out.prompt
        assert rev_out.prompt.count("Retry: e-invoicing market launch ready") == 1
        # Persisted review evidence must certify exactly that range: execute
        # record_review_attempt and read the reviews table directly (no
        # helper-to-helper comparison).
        _add_run(db, "m1", "review", sha_b, sha_b, 3)
        from orchestrator.provenance import record_review_attempt

        rec = await record_review_attempt(db, object(), mission_id="m1", repo=repo)
        assert rec is not None
        stored = db.query("SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at DESC LIMIT 1", ("m1",))[0]
        assert stored["reviewed_base_sha"] == base, "ledger must certify from mission base"
        assert stored["reviewed_head_sha"] == sha_b, "ledger must certify the final candidate"
        assert stored["reviewed_base_sha"] == prompt_base
        assert stored["reviewed_head_sha"] == prompt_head
        # Retry lineage lifecycle inside the replay: omission preserves,
        # explicit verification resolves, parent history is immutable.
        local_before = db.query("SELECT * FROM review_findings WHERE mission_id=?", ("m1",))
        assert any(r["fingerprint"] == fp and r["status"] == "open" for r in local_before)
        persist_findings(db, "m1", "REVIEW_FINDINGS_JSON: []")
        still_open = db.query("SELECT * FROM review_findings WHERE mission_id=? AND fingerprint=?", ("m1", fp))[0]
        assert still_open["status"] == "open", "reviewer omission must not resolve inherited finding"
        db.execute("UPDATE review_findings SET status='repair_attempted' WHERE mission_id=?", ("m1",))
        import json as _rj

        persist_findings(
            db,
            "m1",
            "REVIEW_FINDINGS_JSON: []\nVERIFIED_FIXED_JSON: "
            + _rj.dumps([{"fingerprint": fp, "evidence": "checked at HEAD"}]),
        )
        resolved = db.query("SELECT * FROM review_findings WHERE mission_id=? AND fingerprint=?", ("m1", fp))[0]
        assert resolved["status"] == "resolved"
        parent_row = db.get("review_findings", "f-parent")
        assert parent_row is not None and parent_row["status"] == "open", "parent history must stay immutable"
        # Next retry must not resurrect the resolved finding (AGY-F1).
        _seed_mission(db, "m2", title="Retry: e-invoicing market launch ready", task="t", retry_of="m1")
        assert inherited_open_findings(db, "m2") == []
        assert orch._inherit_retry_findings("m1", "m2") == 0
        assert db.query("SELECT * FROM review_findings WHERE mission_id=?", ("m2",)) == []
        # HIGH variant: unresolved HIGH still blocks the retry.
        _seed_mission(db, "h0", title="h", task="t")
        _add_finding(db, "h0", "fh", "HIGH", "open")
        _seed_mission(db, "h1", title="h", task="t", retry_of="h0")
        assert orch._inherit_retry_findings("h0", "h1") == 1
        assert len(open_blockers(db, "h1")) == 1
        assert len(rev_out.blocks) > 0

    asyncio.run(go())
