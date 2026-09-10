"""Role-specific context compiler (Increment 2).

Deterministic selection, budgeting, role policies, security, versioning.
No provider calls; compilation consumes zero quota.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.context_compiler import (
    POLICY_COMPILED_V2,
    TEMPLATE_COMPILED_V2,
    BlockType,
    ContextCompileError,
    ContextCompiler,
    ContextCompileSpec,
    Priority,
    blocks_to_json,
    budget_for_role,
    context_mode,
    finding_files,
    finding_ids_in_text,
    latest_candidate_shas,
    prepare_invocation_context,
    role_for_stage,
)
from orchestrator.db import Database


def _ensure_project_and_mission(db: Database, mission_id: str = "m-1") -> None:
    try:
        db.insert(
            "projects",
            {
                "id": "proj-1",
                "name": "p",
                "path": "/tmp/gg-test-proj",
                "detected_type": "node",
                "created_at": "2026-01-01T00:00:00",
            },
        )
    except Exception:  # noqa: S110 - idempotent test seeding
        pass
    if not db.get("missions", mission_id):
        db.insert(
            "missions",
            {
                "id": mission_id,
                "project_id": "proj-1",
                "title": "Foundation mission",
                "task": "implement login",
                "status": "IMPLEMENTING",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            },
        )


def _seed_product(db: Database, plan: dict | None = None) -> str:
    _ensure_project_and_mission(db)
    plan = plan or {
        "product_name": "P",
        "goal": "prove compiler",
        "users": "testers",
        "journeys": ["run"],
        "requirements": [
            {
                "id": "R1",
                "title": "Auth works",
                "description": "users can log in",
                "kind": "functional",
                "acceptance": [{"id": "R1-A1", "description": "login returns 200", "verify": "npm run test"}],
            },
            {
                "id": "R2",
                "title": "Styling works",
                "description": "button is blue",
                "kind": "functional",
                "acceptance": [{"id": "R2-A1", "description": "button blue", "verify": "npm run test"}],
            },
            {
                "id": "R4",
                "title": "Input Validation",
                "description": "reject bad titles",
                "kind": "functional",
                "acceptance": [
                    {
                        "id": "R4-A1",
                        "description": "whitespace-only title returns HTTP 400",
                        "verify": "node checks/whitespace-probe.js",
                    }
                ],
            },
        ],
        "non_functional": ["fast"],
        "architecture": {
            "backend": "python",
            "frontend": "react",
            "database": "sqlite",
            "decisions": [
                {"area": "auth", "choice": "session cookies, bcrypt", "rationale": "security"},
                {"area": "styling", "choice": "css modules", "rationale": "isolated styles"},
                {"area": "api", "choice": "REST /api/issues", "rationale": "simple"},
            ],
        },
        "phases": [
            {
                "key": "foundation",
                "title": "Foundation",
                "goal": "auth backend",
                "deliverables": ["login"],
                "tasks": ["implement login"],
                "depends_on": [],
                "workspace_scopes": ["backend"],
                "suggested_providers": [],
                "acceptance": [{"id": "foundation-A1", "description": "done", "verify": "npm run test"}],
                "requirement_ids": ["R1", "R4"],
                "verify_commands": ["npm run test"],
                "human_prerequisites": [],
                "effort": "S",
            },
            {
                "key": "ui",
                "title": "UI",
                "goal": "styling",
                "deliverables": ["button"],
                "tasks": ["style button"],
                "depends_on": ["foundation"],
                "workspace_scopes": ["frontend"],
                "suggested_providers": [],
                "acceptance": [{"id": "ui-A1", "description": "done", "verify": "npm run test"}],
                "requirement_ids": ["R2"],
                "verify_commands": ["npm run test"],
                "human_prerequisites": [],
                "effort": "S",
            },
        ],
        "external_prerequisites": [],
    }
    pid = "prod-1"
    db.insert(
        "product_projects",
        {
            "id": pid,
            "name": "P",
            "idea": "prove compiler",
            "constraints_text": "",
            "state": "EXECUTING",
            "acceptance_state": "",
            "auto_execute": 0,
            "require_plan_approval": 0,
            "target_project_id": None,
            "target_repo_path": "",
            "plan_revision": 1,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        },
    )
    db.insert(
        "plan_revisions",
        {
            "id": "rev-1",
            "project_id": pid,
            "revision": 1,
            "plan_json": plan,
            "created_by": "planner",
            "reason": "test",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    db.insert(
        "project_phases",
        {
            "id": "phase-foundation",
            "project_id": pid,
            "phase_key": "foundation",
            "title": "Foundation",
            "goal": "auth backend",
            "status": "RUNNING",
            "mission_id": "m-1",
            "depends_on": "[]",
            "acceptance_json": "[]",
            "evidence_json": "{}",
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        },
    )
    return pid


def _seed_mission(db: Database, mission_id: str = "m-1") -> None:
    _ensure_project_and_mission(db, mission_id)


def _compiler(tmp_path: Path, cfg_data: dict | None = None) -> tuple[ContextCompiler, Database, Config]:
    db = Database(tmp_path / "c.db")
    _seed_product(db)
    _seed_mission(db)
    cfg = Config(cfg_data or {"context": {"mode": "compiled"}})
    return ContextCompiler(db, cfg), db, cfg


def test_implementer_gets_mapped_requirement_and_acceptance(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        product_project_id="prod-1",
        project_phase_id="phase-foundation",
        mission_id="m-1",
        task_title="Implement login",
        task_description="implement login endpoint",
        requirement_ids=["R4"],
    )
    out = cc.compile(spec)
    assert out.template_version == TEMPLATE_COMPILED_V2
    assert out.policy_version == POLICY_COMPILED_V2
    assert "R4" in out.prompt
    assert "R4-A1" in out.prompt
    assert "whitespace-only title returns HTTP 400" in out.prompt
    assert "node checks/whitespace-probe.js" in out.prompt
    # Irrelevant requirement omitted.
    assert "button is blue" not in out.prompt
    assert "R2-A1" not in out.prompt


def test_phase_inherits_mapped_requirements(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        product_project_id="prod-1",
        project_phase_id="phase-foundation",
        mission_id="m-1",
        task_title="Foundation work",
        task_description="auth backend",
        requirement_ids=[],
    )
    out = cc.compile(spec)
    # Phase foundation maps R1+R4; both must appear even with empty explicit ids.
    assert "R1" in out.prompt
    assert "R4" in out.prompt


def test_architecture_projection_relevant_only(tmp_path: Path):
    cc, _, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        product_project_id="prod-1",
        mission_id="m-1",
        task_title="Style the login button blue",
        task_description="frontend css only",
        requirement_ids=["R2"],
    )
    out = cc.compile(spec)
    # Styling decision relevant; auth may still appear as global but unrelated
    # low-value decisions must not dominate. At minimum styling present.
    assert "css modules" in out.prompt or "styling" in out.prompt.lower()


def test_reviewer_excludes_implementer_self_assessment(tmp_path: Path):
    cc, _, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="reviewer",
        stage="review",
        product_project_id="prod-1",
        mission_id="m-1",
        base_sha="aaa111",
        candidate_sha="bbb222",
        task_title="Review login",
        requirement_ids=["R1"],
        failure_text="Everything is perfect and tests pass. I believe this is ready",
    )
    out = cc.compile(spec)
    assert "Everything is perfect" not in out.prompt
    assert "I believe this is ready" not in out.prompt
    assert "R1" in out.prompt
    assert "bbb222" in out.prompt or "aaa111" in out.prompt


def test_repair_minimality(tmp_path: Path):
    db = Database(tmp_path / "r.db")
    # 10 requirements, 8 historical findings, 1 current.
    reqs = [
        {
            "id": f"R{i}",
            "title": f"Req {i}",
            "description": f"desc {i}",
            "kind": "functional",
            "acceptance": [{"id": f"R{i}-A1", "description": f"works {i}", "verify": "npm run test"}],
        }
        for i in range(1, 11)
    ]
    plan = {
        "product_name": "P",
        "goal": "g",
        "users": "u",
        "requirements": reqs,
        "architecture": {"backend": "py", "frontend": "js", "database": "sqlite", "decisions": []},
        "phases": [
            {
                "key": "k",
                "title": "K",
                "goal": "g",
                "deliverables": [],
                "tasks": [],
                "depends_on": [],
                "workspace_scopes": ["all"],
                "suggested_providers": [],
                "acceptance": [{"id": "k-A1", "description": "d", "verify": "npm run test"}],
                "requirement_ids": [f"R{i}" for i in range(1, 11)],
                "verify_commands": [],
                "human_prerequisites": [],
                "effort": "S",
            }
        ],
    }
    pid = _seed_product(db, plan)
    _seed_mission(db, "m-1")
    for i in range(8):
        db.insert(
            "review_findings",
            {
                "id": f"F{i}",
                "mission_id": "m-1",
                "severity": "MEDIUM",
                "category": "general",
                "description": f"old finding {i}",
                "recommended_fix": "fix it",
                "status": "resolved" if i < 7 else "open",
                "created_at": "2026-01-01T00:00:00",
            },
        )
    cc = ContextCompiler(db, Config({"context": {"mode": "compiled"}}))
    spec = ContextCompileSpec(
        role="repairer",
        stage="repair",
        product_project_id=pid,
        mission_id="m-1",
        candidate_sha="ccc333",
        task_title="Fix R3",
        requirement_ids=["R3"],
        finding_ids=["F7"],
        failure_text="AssertionError: expected 200 got 500",
        failure_command="npm run test",
        failure_exit_code=1,
    )
    out = cc.compile(spec)
    assert "F7" in out.prompt
    assert "R3" in out.prompt
    assert "AssertionError" in out.prompt
    # Unrelated resolved findings omitted.
    assert "old finding 0" not in out.prompt
    assert "old finding 1" not in out.prompt
    # Unrelated requirements omitted.
    assert "Req 9" not in out.prompt


def test_dependency_handoff_included_and_scoped(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    # Seed two DAG tasks; A completed with checkpoint.
    db.insert(
        "tasks",
        {
            "id": "task-a",
            "mission_id": "m-1",
            "role": "implementation",
            "status": "COMPLETED",
            "prompt": "",
            "summary": "added GET /api/issues",
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00",
            "title": "A",
            "description": "api",
            "checkpoint_after": "sha-aaa",
        },
    )
    db.insert(
        "tasks",
        {
            "id": "task-b",
            "mission_id": "m-1",
            "role": "implementation",
            "status": "PENDING",
            "prompt": "",
            "summary": "",
            "attempts": 0,
            "created_at": "2026-01-01T00:00:00",
            "title": "B",
            "description": "consume api",
        },
    )
    db.insert(
        "tasks",
        {
            "id": "task-c",
            "mission_id": "m-1",
            "role": "implementation",
            "status": "PENDING",
            "prompt": "",
            "summary": "",
            "attempts": 0,
            "created_at": "2026-01-01T00:00:00",
            "title": "C",
            "description": "unrelated docs",
        },
    )
    db.insert(
        "task_dependencies", {"from_task_id": "task-a", "to_task_id": "task-b", "created_at": "2026-01-01T00:00:00"}
    )
    spec_b = ContextCompileSpec(
        role="implementer",
        stage="task",
        mission_id="m-1",
        task_id="task-b",
        task_title="B",
        task_description="consume api",
        dependency_ids=["task-a"],
    )
    out_b = cc.compile(spec_b)
    assert "task-a" in out_b.prompt
    assert "sha-aaa" in out_b.prompt
    spec_c = ContextCompileSpec(
        role="implementer",
        stage="task",
        mission_id="m-1",
        task_id="task-c",
        task_title="C",
        task_description="unrelated docs",
        dependency_ids=[],
    )
    out_c = cc.compile(spec_c)
    assert "task-a" not in out_c.prompt


def test_missing_dependency_code_warns(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    db.insert(
        "tasks",
        {
            "id": "dep-1",
            "mission_id": "m-1",
            "role": "implementation",
            "status": "COMPLETED",
            "prompt": "",
            "summary": "",
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00",
            "title": "Dep",
            "description": "d",
        },
    )
    spec = ContextCompileSpec(
        role="implementer",
        stage="task",
        mission_id="m-1",
        task_id="t-2",
        task_title="T",
        dependency_ids=["dep-1"],
    )
    out = cc.compile(spec)
    assert "DEPENDENCY_CODE_NOT_PRESENT" in out.warnings


def test_budget_enforced_and_mandatory_overflow_blocks(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    # Budget just above mandatory but below full preferred set forces omission.
    full_spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        product_project_id="prod-1",
        mission_id="m-1",
        task_title="T",
        task_description="D" * 500,
        requirement_ids=["R1", "R4"],
        workspace_scope=["frontend", "backend"],
        failure_text="prior failure tail " * 50,
    )
    full = cc.compile(full_spec)
    # Pick a budget between mandatory-only and full to force pressure.
    mid_budget = max(150, full.used_estimated_tokens - 100)
    tiny = Config({"context": {"mode": "compiled", "roles": {"implementer": mid_budget}}})
    cc_tiny = ContextCompiler(db, tiny)
    out = cc_tiny.compile(full_spec)
    assert out.used_estimated_tokens <= mid_budget
    assert "R1" in out.prompt  # mandatory preserved
    assert "R4-A1" in out.prompt  # acceptance never dropped for budget
    assert "PROMPT_BUDGET_PRESSURE" in out.warnings
    # Mandatory overflow raises instead of silently dropping acceptance.
    impossible = Config({"context": {"mode": "compiled", "roles": {"implementer": 10}}})
    with pytest.raises(ContextCompileError) as exc:
        ContextCompiler(db, impossible).compile(full_spec)
    assert exc.value.code == "MANDATORY_CONTEXT_OVERFLOW"


def test_prohibited_never_included_and_no_secret_in_manifest(tmp_path: Path):
    from orchestrator.context_compiler import ContextBlock

    cc, _, _ = _compiler(tmp_path)
    # Inject a prohibited block directly through budget selection.
    from orchestrator.context_compiler import _select_with_budget

    secret = "api_key=supersecretvalue12345678"  # noqa: S105 - fake secret for redaction test
    blocks = [
        ContextBlock(id="ok", type=BlockType.TASK_OBJECTIVE, priority=Priority.MANDATORY, content="do work"),
        ContextBlock(id="bad", type=BlockType.TASK_OBJECTIVE, priority=Priority.PROHIBITED, content=secret),
    ]
    _, decisions, _, _ = _select_with_budget(blocks, 16000)
    bad = [d for d in decisions if d.block_id == "bad"][0]
    assert not bad.included
    # Manifest metadata never carries raw secrets.
    raw = blocks_to_json(decisions)
    assert "supersecretvalue" not in raw
    # End-to-end: .env values must not appear in prompt or manifest.
    spec2 = ContextCompileSpec(
        role="implementer",
        stage="task",
        mission_id="m-1",
        task_title="T",
        task_description="do not read .env",
        failure_text="",
    )
    out = cc.compile(spec2)
    assert ".env" in out.prompt  # policy text may mention; values must not
    assert "supersecretvalue" not in out.prompt
    assert "supersecretvalue" not in blocks_to_json(out.blocks)


def test_versioning_and_plan_revision_recorded(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="implementer", stage="task", product_project_id="prod-1", mission_id="m-1", task_title="T"
    )
    out = cc.compile(spec)
    assert out.plan_revision == 1
    assert out.template_version == TEMPLATE_COMPILED_V2
    assert out.policy_version == POLICY_COMPILED_V2


def test_stage_role_mapping():
    assert role_for_stage("product_plan", "planning") == "planner"
    assert role_for_stage("review", "review") == "reviewer"
    assert role_for_stage("repair", "implementer") == "repairer"
    assert role_for_stage("task", "implementation") == "implementer"
    assert role_for_stage("testing", "testing") == "testing"


def test_modes_legacy_compiled_shadow(tmp_path: Path):
    _, db, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(role="implementer", stage="task", mission_id="m-1", task_title="T hello")
    legacy_prompt = "LEGACY PROMPT BODY"
    prompt, meta = prepare_invocation_context(
        legacy_prompt=legacy_prompt, spec=spec, db=db, config=Config({"context": {"mode": "legacy"}})
    )
    assert prompt == legacy_prompt
    assert meta["prompt_template_version"] == "legacy-v1"
    prompt2, meta2 = prepare_invocation_context(
        legacy_prompt=legacy_prompt, spec=spec, db=db, config=Config({"context": {"mode": "compiled"}})
    )
    assert prompt2 != legacy_prompt
    assert meta2["prompt_template_version"] == TEMPLATE_COMPILED_V2
    assert meta2["context_blocks_json"]
    prompt3, meta3 = prepare_invocation_context(
        legacy_prompt=legacy_prompt, spec=spec, db=db, config=Config({"context": {"mode": "shadow"}})
    )
    assert prompt3 == legacy_prompt
    assert "SHADOW_MODE_LEGACY_EXECUTED" in meta3["context_warnings_json"]


def test_budget_for_role_defaults():
    assert budget_for_role("implementer", None) == 16000
    assert budget_for_role("reviewer", None) == 18000
    assert budget_for_role("repairer", None) == 12000
    assert context_mode(Config({"context": {"mode": "shadow"}})) == "shadow"
    assert context_mode(None) == "compiled"


def test_repeated_context_ratio_computed(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(role="implementer", stage="task", mission_id="m-1", task_title="T")
    first = cc.compile(spec)
    # Persist a prior run + manifest so the next compile can overlap.
    db.insert(
        "provider_runs",
        {
            "id": "run-prev",
            "mission_id": "m-1",
            "provider": "fake-a",
            "role": "implementation",
            "command": "[]",
            "cwd": "/tmp",
            "started_at": "2026-01-01T00:00:00",
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "summary": "",
            "stage": "task",
            "run_status": "SUCCEEDED",
            "prompt_template_version": TEMPLATE_COMPILED_V2,
            "context_policy_version": POLICY_COMPILED_V2,
        },
    )
    db.insert(
        "run_context_manifests",
        {
            "run_id": "run-prev",
            "schema_version": "v2",
            "prompt_hash": first.prompt_hash,
            "hash_basis": "redacted_rendered_utf8",
            "prompt_chars": first.prompt_chars,
            "prompt_bytes": first.prompt_bytes,
            "prompt_words": first.prompt_words,
            "estimated_prompt_tokens": first.estimated_tokens,
            "estimator_id": "char4-v1",
            "blocks_json": blocks_to_json(first.blocks),
            "capture_status": "CAPTURED",
            "redaction_status": "REDACTED",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    second = cc.compile(spec)
    assert 0.0 <= second.repeated_context_ratio <= 1.0
    assert second.repeated_context_ratio > 0.0


def test_planner_policy_excludes_history(tmp_path: Path):
    cc, _, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="planner",
        stage="product_plan",
        task_title="New product",
        task_objective="build a todo app",
        extra_context="SCHEMA RULES...",
    )
    out = cc.compile(spec)
    assert "todo app" in out.prompt
    assert "SCHEMA" in out.prompt
    assert "raw historical mission logs" not in out.prompt.lower()


def test_reviewer_gets_diff_metadata(tmp_path: Path):
    cc, _, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(
        role="reviewer",
        stage="review",
        mission_id="m-1",
        task_title="R",
        base_sha="base123",
        candidate_sha="head456",
        git_diff_summary="M src/api.ts",
        git_files_changed=["src/api.ts"],
        requirement_ids=[],
    )
    out = cc.compile(spec)
    assert "base123" in out.prompt
    assert "head456" in out.prompt


def test_reviewer_without_sha_warns(tmp_path: Path):
    cc, _, _ = _compiler(tmp_path)
    spec = ContextCompileSpec(role="reviewer", stage="review", mission_id="m-1", task_title="R")
    out = cc.compile(spec)
    assert "CANDIDATE_SHA_UNKNOWN" in out.warnings


def test_reviewer_sees_repair_attempted_findings(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    db.insert(
        "review_findings",
        {
            "id": "F-verify",
            "mission_id": "m-1",
            "severity": "HIGH",
            "category": "general",
            "file": "src/a.ts",
            "description": "must re-verify after repair",
            "recommended_fix": "check",
            "status": "repair_attempted",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    spec = ContextCompileSpec(
        role="reviewer",
        stage="review",
        mission_id="m-1",
        task_title="R",
        base_sha="b",
        candidate_sha="c",
    )
    out = cc.compile(spec)
    assert "F-verify" in out.prompt
    assert "must re-verify after repair" in out.prompt


def test_reviewer_enriched_with_candidate_range(tmp_path: Path):
    cc, db, _ = _compiler(tmp_path)
    db.insert(
        "provider_runs",
        {
            "id": "run-impl",
            "mission_id": "m-1",
            "provider": "fake-a",
            "role": "implementation",
            "command": "[]",
            "cwd": "/tmp",
            "started_at": "2026-01-01T00:00:00",
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "summary": "did work",
            "stage": "implementation",
            "run_status": "SUCCEEDED",
            "git_commit_before": "base000",
            "git_commit_after": "head111",
        },
    )
    base, cand = latest_candidate_shas(db, "m-1")
    assert (base, cand) == ("base000", "head111")
    assert latest_candidate_shas(db, "missing") == (None, None)
    spec = ContextCompileSpec(
        role="reviewer",
        stage="review",
        mission_id="m-1",
        task_title="R",
        base_sha=base,
        candidate_sha=cand,
        requirement_ids=[],
    )
    out = cc.compile(spec)
    assert "base000" in out.prompt and "head111" in out.prompt


def test_repair_finding_id_extraction(tmp_path: Path):
    assert finding_ids_in_text("## Open findings\n- [HIGH] id=F7 src/a.ts: bad → fix") == ["F7"]
    assert finding_ids_in_text("") == []
    assert finding_ids_in_text(None) == []  # type: ignore[arg-type]
    cc, db, _ = _compiler(tmp_path)
    db.insert(
        "review_findings",
        {
            "id": "F9",
            "mission_id": "m-1",
            "severity": "HIGH",
            "category": "general",
            "file": "src/a.ts",
            "description": "bad",
            "recommended_fix": "fix",
            "status": "open",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    assert finding_files(db, "m-1") == ["src/a.ts"]
    assert finding_files(db, "missing") == []


def test_compiled_prompt_structure_has_contracts(tmp_path: Path):
    cc, _, _ = _compiler(tmp_path)
    for role in ("implementer", "reviewer", "repairer", "testing", "planner"):
        spec = ContextCompileSpec(role=role, stage="task", mission_id="m-1", task_title="T", task_description="D")
        out = cc.compile(spec)
        assert "OUTPUT CONTRACT" in out.prompt
        assert out.prompt_chars > 0
        assert out.estimated_tokens > 0
