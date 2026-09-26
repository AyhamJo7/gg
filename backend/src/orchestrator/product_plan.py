"""Idea-to-Product plan schema, extraction, and validation.

A product plan is produced by a planning provider as structured JSON
(``PRODUCT_PLAN_JSON: {...}`` marker, same fence conventions as the mission
DAG planner). Plans are validated structurally before persistence; malformed
planner output triggers bounded repair, never silent invention.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .criterion import is_executable_command

PLAN_MARKER = "PRODUCT_PLAN_JSON:"
MAX_PLAN_BYTES = 200_000

VALID_SCOPES = frozenset({"frontend", "backend", "data", "docs", "infra", "tests", "all"})


class AcceptanceCriterion(BaseModel):
    id: str
    description: str
    verify: str = ""


class Requirement(BaseModel):
    id: str
    title: str
    description: str = ""
    kind: Literal["functional", "non_functional"] = "functional"
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)


class ArchitectureDecision(BaseModel):
    area: str
    choice: str
    rationale: str = ""


class Architecture(BaseModel):
    frontend: str = ""
    backend: str = ""
    database: str = ""
    auth: str = ""
    api_design: str = ""
    integrations: list[str] = Field(default_factory=list)
    deployment: str = ""
    testing_strategy: str = ""
    security_notes: str = ""
    repo_structure: str = ""
    dependency_strategy: str = ""
    decisions: list[ArchitectureDecision] = Field(default_factory=list)


class PhaseSpec(BaseModel):
    key: str
    title: str
    goal: str = ""
    deliverables: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    workspace_scopes: list[str] = Field(default_factory=list)
    suggested_providers: list[str] = Field(default_factory=list)
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    human_prerequisites: list[str] = Field(default_factory=list)
    effort: str = "M"


class ExternalPrerequisite(BaseModel):
    key: str
    title: str
    what_required: str = ""
    why_required: str = ""
    human_action: str = ""
    where_to_provide: str = ""
    validation: str = ""
    required_vars: list[str] = Field(default_factory=list)


class ProductPlan(BaseModel):
    product_name: str
    goal: str = ""
    users: str = ""
    journeys: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    non_functional: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    architecture: Architecture = Field(default_factory=Architecture)
    phases: list[PhaseSpec] = Field(default_factory=list)
    external_prerequisites: list[ExternalPrerequisite] = Field(default_factory=list)


def extract_product_plan(text: str) -> dict[str, Any] | None:
    """Extract the plan JSON following the PRODUCT_PLAN_JSON marker."""
    idx = text.find(PLAN_MARKER)
    if idx < 0:
        return None
    payload = text[idx + len(PLAN_MARKER) :].strip()
    if payload.startswith("```"):
        lines = payload.splitlines()
        payload = "\n".join(lines[1:])
        end = payload.rfind("```")
        if end >= 0:
            payload = payload[:end]
    payload = payload.strip()
    if len(payload.encode("utf-8")) > MAX_PLAN_BYTES:
        return None
    # Balanced-brace scan so trailing prose does not break parsing.
    start = payload.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(payload)):
        ch = payload[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(payload[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def validate_product_plan(data: dict[str, Any]) -> list[str]:
    """Return a list of structural problems; empty means valid."""
    errors: list[str] = []
    try:
        plan = ProductPlan.model_validate(data)
    except ValidationError as exc:
        return [f"schema: {e['loc'] and '.'.join(str(p) for p in e['loc'])} {e['msg']}" for e in exc.errors()]
    if not plan.product_name.strip():
        errors.append("product_name is required")
    if not plan.requirements:
        errors.append("at least one requirement is required")
    req_ids = [r.id for r in plan.requirements]
    dupes = sorted({r for r in req_ids if req_ids.count(r) > 1})
    if dupes:
        errors.append(f"duplicate requirement ids: {', '.join(dupes)}")
    seen_criteria: dict[str, str] = {}
    for r in plan.requirements:
        if not r.acceptance:
            errors.append(f"requirement {r.id} has no acceptance criteria")
        for a in r.acceptance:
            if not a.id.strip():
                errors.append(f"requirement {r.id} has a criterion with a blank id")
            elif a.id in seen_criteria:
                errors.append(f"duplicate criterion id: {a.id} (in {seen_criteria[a.id]} and {r.id})")
            else:
                seen_criteria[a.id] = r.id
            ok, _ = is_executable_command(a.verify)
            if not ok:
                errors.append(f"criterion {a.id} verify is not an executable allowlisted command: {a.verify[:100]!r}")
    for p in plan.phases:
        for a in p.acceptance:
            if not a.id.strip():
                errors.append(f"phase {p.key} has a criterion with a blank id")
            elif a.id in seen_criteria:
                errors.append(f"duplicate criterion id: {a.id} (already used; phase {p.key})")
            else:
                seen_criteria[a.id] = f"phase:{p.key}"
    if not plan.phases:
        errors.append("at least one phase is required")
    phase_keys = [p.key for p in plan.phases]
    dup_phases = sorted({k for k in phase_keys if phase_keys.count(k) > 1})
    if dup_phases:
        errors.append(f"duplicate phase keys: {', '.join(dup_phases)}")
    phase_set = set(phase_keys)
    for p in plan.phases:
        for dep in p.depends_on:
            if dep not in phase_set:
                errors.append(f"phase {p.key} depends on unknown phase {dep}")
        for scope in p.workspace_scopes:
            if scope not in VALID_SCOPES:
                errors.append(f"phase {p.key} has invalid scope {scope!r}")
        if not p.acceptance:
            errors.append(f"phase {p.key} has no acceptance criteria")
        for rid in p.requirement_ids:
            if rid not in req_ids:
                errors.append(f"phase {p.key} references unknown requirement {rid}")
    # Cycle detection over phase dependencies.
    visiting: set[str] = set()
    visited: set[str] = set()
    graph = {p.key: [d for d in p.depends_on if d in phase_set] for p in plan.phases}

    def visit(node: str, stack: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            errors.append(f"dependency cycle: {' -> '.join([*stack, node])}")
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            visit(dep, [*stack, node])
        visiting.discard(node)
        visited.add(node)

    for key in phase_set:
        visit(key, [])
    covered = {rid for p in plan.phases for rid in p.requirement_ids}
    for rid in req_ids:
        if rid not in covered:
            errors.append(f"requirement {rid} is not covered by any phase")
    prereq_keys = {e.key for e in plan.external_prerequisites}
    for p in plan.phases:
        for pre in p.human_prerequisites:
            if pre not in prereq_keys:
                errors.append(f"phase {p.key} references unknown prerequisite {pre}")
    for e in plan.external_prerequisites:
        validation = e.validation.strip()
        # Only an explicit "run <command>" validation is ever executed (see
        # ProjectCoordinator.resolve_gate); anything else — empty, or prose
        # like "presence of TEST_TOKEN in .env" — is human-attested and never
        # runs, so it needs no command validation.
        if not validation.lower().startswith("run "):
            continue
        ok, _ = is_executable_command(validation[4:].strip())
        if not ok:
            errors.append(
                f"prerequisite {e.key} validation is not an executable allowlisted command: {validation[:100]!r}"
            )
    return errors


def build_planner_prompt(idea: str, constraints: str, workspace_hint: str = "") -> str:
    return f"""You are the product planner for a local software-development orchestrator.
Turn the user's idea into a COMPLETE product plan covering the entire MVP.

USER IDEA:
{idea}

CONSTRAINTS:
{constraints or "(none)"}

TARGET WORKSPACE: {workspace_hint or "(a fresh local git repository will be created)"}

RULES:
- Prefer a simple maintainable architecture (monolith or small split, local-first).
- Never invent microservices, cloud infrastructure, or paid dependencies unless
  the idea genuinely requires them.
- Every requirement needs measurable acceptance criteria.
- Every criterion needs an EXECUTABLE verification command, matching ONE of
  these exact shapes (anything else is rejected, no exceptions):
  "npm test", "npm ci", "npm install", "npm run <script>" (npm only, always,
  regardless of what the project's package.json otherwise uses); "node
  <path>.js" (a single script file, no flags);
  "python3 <path>.py" or "python3 -m {{pytest, unittest, mypy, ruff}}" (a single
  script file or one of those four modules only — no other -m target, no
  -c/-e/--eval, no other flags); "pytest" with an in-repo path and/or -k/-m/
  -q/-v/-x/-s/--tb= flags; "go test ./...", "go test ./<path>", "go build
  ./...", "go build ./<path>", or "go run ./<path>"; "cargo test" or "cargo
  build" (optionally "--release"; "cargo run" is never accepted); "make
  <target>" (bare target name, no flags); or "uv run <inner>" wrapping one of
  the shapes above. Write a checks/*.js or checks/*.py FILE and run it as a
  file argument rather than inline code. GG runs each command in the target
  repository and a criterion passes only on exit 0. For HTTP behavior, write
  a small script file that starts a server on an ephemeral port, probes it,
  and exits non-zero on failure. Example: "node checks/whitespace-probe.js".
  Prose is not verification.
- Every phase needs acceptance criteria and must reference the requirement ids
  it implements. Every requirement must be covered by at least one phase.
- Phase keys must be unique; depends_on may only reference other phase keys;
  no cycles. Workspace scopes: frontend, backend, data, docs, infra, tests, all.
- List anything you cannot do yourself (credentials, accounts, external setup)
  under external_prerequisites with exact human actions — never ask the human
  for work you can safely do yourself. Its "validation" field is either
  descriptive prose (human-attested, never executed) or, if it can be checked
  automatically, "run " followed by the same kind of executable allowlisted
  command as above — never inline code, never rm/sudo/git.
- Respond with a single PRODUCT_PLAN_JSON block and nothing else after it.

SCHEMA (all listed fields; omit nothing required):
{{
  "product_name": "...",
  "goal": "...",
  "users": "...",
  "journeys": ["..."],
  "requirements": [{{"id": "R1", "title": "...", "description": "...",
    "kind": "functional",
    "acceptance": [{{"id": "R1-A1", "description": "...", "verify": "..."}}]}}],
  "non_functional": ["..."],
  "assumptions": ["..."],
  "out_of_scope": ["..."],
  "risks": ["..."],
  "architecture": {{"frontend": "...", "backend": "...", "database": "...",
    "auth": "...", "api_design": "...", "integrations": [],
    "deployment": "...", "testing_strategy": "...", "security_notes": "...",
    "repo_structure": "...", "dependency_strategy": "...",
    "decisions": [{{"area": "...", "choice": "...", "rationale": "..."}}]}},
  "phases": [{{"key": "foundation", "title": "...", "goal": "...",
    "deliverables": ["..."], "tasks": ["..."], "depends_on": [],
    "workspace_scopes": ["all"], "suggested_providers": [],
    "acceptance": [{{"id": "foundation-A1", "description": "...", "verify": "..."}}],
    "requirement_ids": ["R1"], "verify_commands": ["..."],
    "human_prerequisites": [], "effort": "S"}}],
  "external_prerequisites": [{{"key": "stripe-test", "title": "...",
    "what_required": "...", "why_required": "...", "human_action": "...",
    "where_to_provide": "...", "validation": "...", "required_vars": []}}]
}}

PRODUCT_PLAN_JSON:
"""


def build_plan_repair_prompt(previous_output: str, errors: list[str]) -> str:
    bullets = "\n".join(f"- {e}" for e in errors)
    return f"""Your previous product plan was structurally invalid. Fix EXACTLY the
problems below and output a corrected single PRODUCT_PLAN_JSON block.
Do not invent unrelated changes; keep every valid part of the previous plan.

PROBLEMS:
{bullets}

PREVIOUS OUTPUT:
{previous_output[:6000]}

PRODUCT_PLAN_JSON:
"""
