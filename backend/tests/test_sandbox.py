"""Tests for the bwrap-based sandboxed execution layer.

This is the actual repo-confinement boundary for planner-authored verify/
gate commands, replacing an approach (six rounds of it) that tried to
statically prove such a command couldn't escape the repo via its own
manifest content — see criterion.py's module docstring and sandbox.py's
module docstring for why that never converged. These tests prove
containment empirically, the same way the reviews that motivated this
module proved bypasses empirically: real toolchains, real escape attempts,
real assertions that nothing outside the repo was touched.

Skipped entirely when bubblewrap isn't installed — there is no
unsandboxed fallback to test instead.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from orchestrator.sandbox import run_sandboxed, sandbox_available

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="bubblewrap (bwrap) not installed")


# -- basic containment properties -------------------------------------------


async def test_legit_command_runs_and_sees_repo(tmp_path: Path):
    (tmp_path / "file.txt").write_text("hello")
    result = await run_sandboxed(["cat", "file.txt"], tmp_path, timeout_s=10)
    assert result.exit_code == 0
    assert "hello" in result.combined_tail


async def test_cannot_read_a_file_outside_the_repo(tmp_path: Path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("secret-content")
    try:
        result = await run_sandboxed(["cat", str(outside)], tmp_path, timeout_s=10)
        assert result.exit_code != 0
        assert "secret-content" not in result.combined_tail
    finally:
        outside.unlink(missing_ok=True)


async def test_network_is_unreachable(tmp_path: Path):
    script = (
        "import socket\n"
        "s = socket.socket()\n"
        "s.settimeout(2)\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 80))\n"
        "    print('NETWORK_WORKED')\n"
        "except OSError:\n"
        "    print('NETWORK_BLOCKED')\n"
    )
    result = await run_sandboxed(["python3", "-c", script], tmp_path, timeout_s=10)
    assert "NETWORK_BLOCKED" in result.combined_tail
    assert "NETWORK_WORKED" not in result.combined_tail


async def test_credential_file_is_masked(tmp_path: Path):
    npmrc = Path.home() / ".npmrc"
    if not npmrc.is_file():
        pytest.skip("no ~/.npmrc on this machine to verify masking against")
    result = await run_sandboxed(["cat", str(npmrc)], tmp_path, timeout_s=10)
    assert result.exit_code != 0


async def test_env_is_cleared_except_forwarded_vars(tmp_path: Path):
    result = await run_sandboxed(["python3", "-c", "import os; print(sorted(os.environ))"], tmp_path, timeout_s=10)
    assert result.exit_code == 0
    # A representative unrelated secret-shaped var should never appear —
    # --clearenv means only the explicitly forwarded/set names exist at all.
    assert "ANTHROPIC_API_KEY" not in result.combined_tail
    assert "AWS_SECRET_ACCESS_KEY" not in result.combined_tail


# -- legitimate usage still works --------------------------------------------


async def test_make_target_runs(tmp_path: Path):
    (tmp_path / "Makefile").write_text("hello:\n\techo hello-from-make\n")
    result = await run_sandboxed(["make", "hello"], tmp_path, timeout_s=30)
    assert result.exit_code == 0
    assert "hello-from-make" in result.combined_tail


async def test_node_script_runs(tmp_path: Path):
    (tmp_path / "ok.js").write_text("console.log('node-ok')")
    result = await run_sandboxed(["node", "ok.js"], tmp_path, timeout_s=30)
    assert result.exit_code == 0
    assert "node-ok" in result.combined_tail


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
async def test_npm_test_runs(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "t", "version": "1.0.0", "scripts": {"test": "node -e \"console.log('npm-test-ok')\""}})
    )
    result = await run_sandboxed(["npm", "test"], tmp_path, timeout_s=60)
    assert result.exit_code == 0
    assert "npm-test-ok" in result.combined_tail


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
async def test_cargo_test_runs(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "legit"\nversion = "0.1.0"\nedition = "2021"\n')
    (tmp_path / "src" / "lib.rs").write_text("#[test]\nfn it_works() { assert_eq!(2 + 2, 4); }\n")
    result = await run_sandboxed(["cargo", "test"], tmp_path, timeout_s=120)
    assert result.exit_code == 0
    assert "test result: ok" in result.combined_tail


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
async def test_uv_run_pytest_uses_preexisting_venv(tmp_path: Path):
    """Verify commands run after the (separate, network-enabled) install
    step, so `uv run pytest` only needs the already-synced venv — proven
    here by actually syncing one first, then running fully offline inside
    the sandbox."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "t"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = ["pytest>=8"]\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")

    sync = await asyncio.create_subprocess_exec(
        "uv", "sync", "-q", cwd=str(tmp_path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await sync.wait()
    assert sync.returncode == 0, "uv sync (outside the sandbox, network-enabled) must succeed for this test to be valid"

    result = await run_sandboxed(["uv", "run", "pytest", "-q"], tmp_path, timeout_s=60)
    assert result.exit_code == 0
    assert "1 passed" in result.combined_tail


# -- proven-live manifest-driven escapes, now contained ----------------------
#
# Both scenarios below were proven to actually execute code outside the repo
# against the previous manifest-content-enumeration design (round 6->7
# security review, with real npm/cargo on this machine) — neither
# "workspaces" nor a workspace member's own Cargo.toml was ever inspected by
# that design. The sandbox closes both without needing to have known either
# mechanism existed.


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
async def test_npm_workspaces_escape_is_contained(tmp_path: Path):
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
    await run_sandboxed(["npm", "install", "--no-audit", "--no-fund"], repo, timeout_s=60)
    assert not (repo / "MARKER_ESCAPED").exists()
    assert not (outside / "MARKER_ESCAPED").exists()


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
async def test_cargo_workspace_member_escape_is_contained(tmp_path: Path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside-evil2"
    repo.mkdir()
    outside.mkdir()
    (outside / "Cargo.toml").write_text('[package]\nname = "outside-evil2"\nversion = "0.1.0"\nbuild = "build.rs"\n')
    (outside / "build.rs").write_text('fn main() { std::fs::write("MARKER_ESCAPED", "pwned").unwrap(); }')
    (outside / "src").mkdir()
    (outside / "src" / "lib.rs").write_text("")
    member = repo / "crates" / "member"
    member.mkdir(parents=True)
    (member / "Cargo.toml").write_text(
        '[package]\nname = "member"\nversion = "0.1.0"\n\n'
        '[dependencies]\noutside-evil2 = { path = "../../../outside-evil2" }\n'
    )
    (member / "src").mkdir()
    (member / "src" / "lib.rs").write_text("")
    (repo / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/member"]\n')

    result = await run_sandboxed(["cargo", "build"], repo, timeout_s=120)
    assert result.exit_code != 0, "build should fail cleanly (dependency not found), never escape"
    assert not (repo / "MARKER_ESCAPED").exists()
    assert not (outside / "MARKER_ESCAPED").exists()
