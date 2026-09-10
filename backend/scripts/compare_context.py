"""Legacy-v1 vs compiled-v2 structural benchmark (Increment 2).

Deterministic, no provider calls. Reconstructs representative legacy prompts
from current builders and compiles the same semantic task with the
Role-Specific Context Compiler. Reports chars, char4-v1 estimates, deltas,
repeated-context ratios, and mandatory-context preservation.

Usage:
    PYTHONPATH=backend/src backend/.venv/bin/python backend/scripts/compare_context.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.config import Config  # noqa: E402
from orchestrator.context_compiler import (  # noqa: E402
    ContextCompiler,
    ContextCompileSpec,
)
from orchestrator.context_manifest import estimate_tokens  # noqa: E402
from orchestrator.db import Database  # noqa: E402
from orchestrator.product_plan import build_planner_prompt  # noqa: E402


def _seed(db: Database) -> str:
    db.insert(
        "projects",
        {"id": "proj-1", "name": "p", "path": "/tmp/gg-bench", "detected_type": "node", "created_at": "t"},  # noqa: S108
    )
    db.insert(
        "missions",
        {
            "id": "m-1",
            "project_id": "proj-1",
            "title": "Foundation mission",
            "task": "implement login with input validation",
            "status": "IMPLEMENTING",
            "created_at": "t",
            "updated_at": "t",
        },
    )
    pid = "prod-1"
    plan = {
        "product_name": "Bench",
        "goal": "prove compiler",
        "users": "testers",
        "requirements": [
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
            {
                "id": "R9",
                "title": "Unrelated",
                "description": "unrelated req",
                "kind": "functional",
                "acceptance": [{"id": "R9-A1", "description": "unrelated", "verify": "npm run test"}],
            },
        ],
        "non_functional": [],
        "architecture": {
            "backend": "python",
            "frontend": "react",
            "database": "sqlite",
            "decisions": [
                {"area": "auth", "choice": "session cookies", "rationale": "security"},
                {"area": "styling", "choice": "css modules", "rationale": "isolated"},
            ],
        },
        "phases": [
            {
                "key": "foundation",
                "title": "Foundation",
                "goal": "auth",
                "deliverables": [],
                "tasks": [],
                "depends_on": [],
                "workspace_scopes": ["backend"],
                "suggested_providers": [],
                "acceptance": [{"id": "f-A1", "description": "d", "verify": "npm run test"}],
                "requirement_ids": ["R4"],
                "verify_commands": [],
                "human_prerequisites": [],
                "effort": "S",
            }
        ],
    }
    db.insert(
        "product_projects",
        {
            "id": pid,
            "name": "Bench",
            "idea": "prove compiler",
            "constraints_text": "",
            "state": "EXECUTING",
            "acceptance_state": "",
            "auto_execute": 0,
            "require_plan_approval": 0,
            "target_project_id": None,
            "target_repo_path": "",
            "plan_revision": 1,
            "created_at": "t",
            "updated_at": "t",
        },
    )
    db.insert(
        "plan_revisions",
        {
            "id": "r1",
            "project_id": pid,
            "revision": 1,
            "plan_json": plan,
            "created_by": "t",
            "reason": "t",
            "created_at": "t",
        },
    )
    db.insert(
        "project_phases",
        {
            "id": "phase-1",
            "project_id": pid,
            "phase_key": "foundation",
            "title": "Foundation",
            "goal": "auth",
            "status": "RUNNING",
            "mission_id": "m-1",
            "depends_on": "[]",
            "acceptance_json": "[]",
            "evidence_json": "{}",
            "attempts": 1,
            "created_at": "t",
            "updated_at": "t",
        },
    )
    return pid


def est(text: str) -> int:
    return estimate_tokens(text).tokens


def main() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="gg-ctx-bench-"))
    db = Database(tmp / "bench.db")
    pid = _seed(db)
    cfg = Config({"context": {"mode": "compiled"}})
    cc = ContextCompiler(db, cfg)

    # Representative legacy prompts (current builders, pre-compiler shapes).
    legacy_planner = build_planner_prompt("prove compiler", "")
    legacy_implementer = (
        "You are working as the **implementation** engineer.\n\n"
        "Foundation mission\nimplement login with input validation\n" * 20
    )
    legacy_reviewer = legacy_implementer + "\nREVIEW_INSTRUCTIONS " + "contract " * 200
    legacy_repair = legacy_implementer + "\nFailed: AssertionError " + "history " * 300

    cases = [
        (
            "planner",
            legacy_planner,
            ContextCompileSpec(
                role="planner",
                stage="product_plan",
                product_project_id=pid,
                task_title="Bench",
                task_objective="prove compiler",
                extra_context=legacy_planner,
            ),
        ),
        (
            "implementer",
            legacy_implementer,
            ContextCompileSpec(
                role="implementer",
                stage="implementation",
                product_project_id=pid,
                project_phase_id="phase-1",
                mission_id="m-1",
                task_title="Implement login",
                task_description="implement login with input validation",
                requirement_ids=["R4"],
            ),
        ),
        (
            "reviewer",
            legacy_reviewer,
            ContextCompileSpec(
                role="reviewer",
                stage="review",
                product_project_id=pid,
                mission_id="m-1",
                base_sha="base000",
                candidate_sha="head111",
                task_title="Review login",
                requirement_ids=["R4"],
                git_diff_summary="M src/api.ts",
                git_files_changed=["src/api.ts"],
            ),
        ),
        (
            "repairer",
            legacy_repair,
            ContextCompileSpec(
                role="repairer",
                stage="repair",
                product_project_id=pid,
                mission_id="m-1",
                candidate_sha="head111",
                task_title="Fix R4-A1",
                requirement_ids=["R4"],
                failure_text="AssertionError: expected 400 got 200",
                failure_command="node checks/whitespace-probe.js",
                failure_exit_code=1,
            ),
        ),
    ]
    print("role | legacy_chars | compiled_chars | legacy_est | compiled_est | delta_est | repeated | mandatory_ok")
    for role, legacy, spec in cases:
        compiled = cc.compile(spec)
        repeated = f"{compiled.repeated_context_ratio:.3f}"
        mandatory_ok = all(
            k in compiled.prompt
            for k in (["R4", "R4-A1"] if role in ("implementer", "reviewer", "repairer") else ["prove compiler"])
        )
        print(
            f"{role} | {len(legacy)} | {compiled.prompt_chars} | {est(legacy)} "
            f"| {compiled.estimated_tokens} | {compiled.estimated_tokens - est(legacy)} "
            f"| {repeated} | {mandatory_ok}"
        )
    print(f"db={tmp / 'bench.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
