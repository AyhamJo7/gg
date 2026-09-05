"""Structured handoff documents.

Each provider switch or phase transition produces a compact markdown document
giving the next provider everything it needs without the full chat history.
Persisted to SQLite and to <project>/.orchestrator/handoffs/.
"""

from __future__ import annotations

from pathlib import Path

from .models import Handoff, Mission, utcnow


def render_handoff(
    mission: Mission,
    role: str,
    from_provider: str | None,
    to_provider: str | None,
    workspace_summary: str,
    completed_work: list[str],
    tests: list[str],
    review_findings: list[str],
    next_action: str,
    git_head: str | None,
    constraints: list[str] | None = None,
) -> str:
    constraints = constraints or [
        "Never commit secrets or .env files.",
        "Do not use destructive git commands (reset --hard, clean -fd).",
        "Keep changes minimal and focused on the current goal.",
    ]
    completed = "\n".join(f"- {w}" for w in completed_work) or "- (nothing yet)"
    tests_text = "\n".join(f"- {t}" for t in tests) or "- none run yet"
    findings = "\n".join(f"- {f}" for f in review_findings) or "- none"
    constraints_text = "\n".join(f"- {c}" for c in constraints)
    return f"""# Mission Handoff

## Mission
{mission.title}

## Original requirements
{mission.task}

## Current goal (role: {role})
{next_action}

## Provider chain
from: {from_provider or 'orchestrator'} → to: {to_provider or 'next selected provider'}
providers used so far: {', '.join(mission.providers_used) or 'none'}
providers failed: {', '.join(mission.providers_failed) or 'none'}

## Workspace
{workspace_summary}

## Completed work
{completed}

## Tests executed
{tests_text}

## Open review findings
{findings}

## Current Git commit
{git_head or '(no commits yet)'}

## Next exact action
{next_action}

## Constraints / do-not-repeat
{constraints_text}
"""


def persist_handoff(
    db: object,
    project_path: Path,
    mission: Mission,
    role: str,
    content: str,
    from_provider: str | None,
    to_provider: str | None,
    git_head: str | None,
) -> Handoff:
    handoff_dir = project_path / ".orchestrator" / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff = Handoff(
        mission_id=mission.id,
        from_provider=from_provider,
        to_provider=to_provider,
        role=role,
        content=content,
        git_head=git_head,
    )
    filename = f"{utcnow().strftime('%Y%m%dT%H%M%S')}_{role}_{handoff.id}.md"
    path = handoff_dir / filename
    path.write_text(content)
    handoff.path = str(path)
    db.insert(  # type: ignore[attr-defined]
        "handoffs",
        {
            "id": handoff.id,
            "mission_id": handoff.mission_id,
            "from_provider": handoff.from_provider,
            "to_provider": handoff.to_provider,
            "role": handoff.role,
            "content": handoff.content,
            "path": handoff.path,
            "git_head": handoff.git_head,
            "created_at": handoff.created_at,
        },
    )
    return handoff
