"""verify.py must route command execution through the sandbox, not
process.run_process directly — this is the dominant verification path
(every mission's FINAL_VALIDATION, plus product acceptance and
fresh-checkout reproduction), and it runs exactly the command shapes
(``npm test``, ``cargo build``) proven exploitable via manifest-driven
escapes when unsandboxed. These tests prove the wiring itself, not just
that sandbox.run_sandboxed works in isolation (test_sandbox.py already
covers that) — through the real run_verification() call path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.sandbox import sandbox_available
from orchestrator.verify import run_verification
from orchestrator.workspace import WorkspaceInfo

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="bubblewrap (bwrap) not installed")


def _events(tmp_path: Path) -> EventBus:
    return EventBus(Database(tmp_path / "events.db"))


async def test_run_verification_uses_sandbox_for_legit_command(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(
        json.dumps({"name": "t", "scripts": {"test": "node -e \"console.log('verify-ok')\""}})
    )
    info = WorkspaceInfo(path=ws, is_git_repo=False, test_commands=["npm test"])
    report = await run_verification(info, db=None, events=_events(tmp_path), mission_id="m1", workdir=ws)
    assert report.all_passed
    assert "verify-ok" in report.results[0].tail


async def test_run_verification_contains_npm_workspaces_escape(tmp_path: Path):
    """Reproduces the exact proven-live bypass (npm "workspaces" pointing
    outside the repo, postinstall payload) through run_verification()
    itself — proves FINAL_VALIDATION/acceptance can't be used to reach
    the escape that criterion.py's equivalent test already proved closed
    for the criterion/gate-validation call sites.
    """
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-workspace"
    repo.mkdir()
    outside.mkdir()
    (outside / "package.json").write_text(
        json.dumps(
            {
                "name": "evil-workspace",
                "version": "1.0.0",
                "scripts": {"postinstall": "node -e \"require('fs').writeFileSync('MARKER_ESCAPED','pwned')\""},
            }
        )
    )
    (repo / "package.json").write_text(
        json.dumps({"name": "repo", "version": "1.0.0", "private": True, "workspaces": ["../outside-workspace"]})
    )
    info = WorkspaceInfo(path=repo, is_git_repo=False, test_commands=["npm install --no-audit --no-fund"])
    await run_verification(info, db=None, events=_events(tmp_path), mission_id="m2", workdir=repo)
    assert not (repo / "MARKER_ESCAPED").exists()
    assert not (outside / "MARKER_ESCAPED").exists()


async def test_run_verification_fails_closed_without_sandbox(tmp_path: Path, monkeypatch):
    """When bwrap is unavailable, verification must be reported as failed
    with a clear reason — never silently run unsandboxed."""
    monkeypatch.setattr("orchestrator.verify.sandbox_available", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    info = WorkspaceInfo(path=ws, is_git_repo=False, test_commands=["echo should-not-run"])
    report = await run_verification(info, db=None, events=_events(tmp_path), mission_id="m3", workdir=ws)
    assert report.attempted
    assert not report.all_passed
    assert report.results[0].exit_code is None
    assert "bubblewrap" in report.results[0].tail
