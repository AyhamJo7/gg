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
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from orchestrator.sandbox import build_sandboxed_argv, run_sandboxed, sandbox_available

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


async def test_sandboxed_process_cannot_signal_a_host_process(tmp_path: Path):
    """Proven live during round-8 review: a shared PID namespace let a
    sandboxed command kill(2) an arbitrary host process by pid, including
    the orchestrator's own, despite no filesystem/network access. Must be
    unreachable now that --unshare-pid is applied."""
    victim = await asyncio.create_subprocess_exec("sleep", "30")
    try:
        await asyncio.sleep(0.3)
        result = await run_sandboxed(["kill", "-TERM", str(victim.pid)], tmp_path, timeout_s=10)
        assert result.exit_code != 0
        await asyncio.sleep(0.3)
        assert victim.returncode is None, "host process must survive a signal attempt from inside the sandbox"
    finally:
        victim.kill()
        await victim.wait()


async def test_process_count_ulimit_is_applied(tmp_path: Path):
    result = await run_sandboxed(["bash", "-c", "ulimit -u"], tmp_path, timeout_s=10)
    assert result.exit_code == 0
    assert result.combined_tail.strip() == "2048"


async def test_cache_dirs_outside_the_allowlist_are_not_bound(tmp_path: Path, monkeypatch):
    """The allow-list replaced wholesale ~/.cache/~/.local binds after
    those were proven to expose a live Hugging Face token and a live
    Jupyter session secret on a real machine — neither a toolchain path.
    Reproduces the same shape (a secret-looking file under an
    unenumerated .cache subdirectory) against a synthetic fake $HOME, so
    this doesn't touch the real developer home directory or depend on
    what happens to exist on whichever machine runs this test."""
    fake_home = tmp_path / "fake-home"
    repo = tmp_path / "repo"  # sibling of fake_home, not an ancestor — the repo!=home guard must not fire
    repo.mkdir()
    (fake_home / ".cache" / "uv").mkdir(parents=True)  # on the allow-list: must stay reachable
    (fake_home / ".cache" / "some-other-tool").mkdir(parents=True)  # not on the allow-list
    secret = fake_home / ".cache" / "some-other-tool" / "token"
    secret.write_text("should-never-be-readable-inside-the-sandbox")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    result = await run_sandboxed(["cat", str(secret)], repo, timeout_s=10)
    assert result.exit_code != 0
    assert "should-never-be-readable" not in result.combined_tail

    allowed = await run_sandboxed(["ls", str(fake_home / ".cache" / "uv")], repo, timeout_s=10)
    assert allowed.exit_code == 0, "the allow-listed subpath itself must still be reachable"


def test_sandboxed_path_excludes_unbound_host_directories(tmp_path: Path):
    argv = build_sandboxed_argv(["true"], tmp_path, tmp_path / "scratch")
    path_value = None
    for i, tok in enumerate(argv):
        if tok == "--setenv" and i + 1 < len(argv) and argv[i + 1] == "PATH":
            path_value = argv[i + 2]
            break
    assert path_value is not None
    fake_unbound = "/definitely/not/a/bound/directory/bin"
    assert fake_unbound not in path_value.split(":")


def _path_env(argv: list[str]) -> str:
    for i, tok in enumerate(argv):
        if tok == "--setenv" and i + 1 < len(argv) and argv[i + 1] == "PATH":
            return argv[i + 2]
    raise AssertionError("PATH was not set in the built argv")


def test_toolchain_dirs_precede_system_dirs_in_path(tmp_path: Path, monkeypatch):
    """A stray system-package binary (e.g. apt's /usr/bin/node) must never
    shadow a project's version-managed toolchain (e.g. nvm's active node) —
    proven on a real machine where /usr/bin/node (v20.20.0) and nvm's active
    node (v22.20.0, the version a project's .nvmrc/packageManager actually
    expects) coexist. Placing system dirs first in PATH, as an earlier
    version of _build_sandboxed_path did, resolved `node`/`corepack` to the
    wrong, unmanaged binary. This must hold regardless of which directory
    happens to appear first in the *host's* PATH."""
    fake_home = tmp_path / "fake-home"
    nvm_bin = fake_home / ".nvm" / "versions" / "node" / "v22.0.0" / "bin"
    nvm_bin.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PATH", f"/usr/bin:{nvm_bin}")  # system dir listed first in the *host* PATH

    argv = build_sandboxed_argv(["true"], repo, tmp_path / "scratch")
    entries = _path_env(argv).split(":")
    assert entries.index(str(nvm_bin)) < entries.index("/usr/bin"), (
        "toolchain-managed directory must precede the generic system directory "
        "in the sandboxed PATH regardless of host PATH order"
    )


def test_corepack_home_repointed_to_real_cache_when_present(tmp_path: Path, monkeypatch):
    """corepack (bundled with Node, provisions pnpm/yarn on first use) reads
    COREPACK_HOME directly and otherwise derives a HOME-relative default;
    since the sandbox always overrides HOME to an empty scratch dir, an
    unset COREPACK_HOME makes corepack see an empty cache on every run and
    try to fetch the pinned package manager over network — unavailable
    during verify-time sandboxing. Same re-point pattern as CARGO_HOME/
    RUSTUP_HOME/GOPATH."""
    fake_home = tmp_path / "fake-home"
    corepack_cache = fake_home / ".cache" / "node" / "corepack"
    corepack_cache.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    argv = build_sandboxed_argv(["true"], repo, tmp_path / "scratch")
    assert "COREPACK_HOME" in argv
    assert argv[argv.index("COREPACK_HOME") + 1] == str(corepack_cache)


def test_corepack_home_not_set_when_no_cache_exists(tmp_path: Path, monkeypatch):
    """No false re-point to a nonexistent path — mirrors CARGO_HOME/RUSTUP_HOME's
    existing is_dir() guard, not a new behavior for this variable."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    argv = build_sandboxed_argv(["true"], repo, tmp_path / "scratch")
    assert "COREPACK_HOME" not in argv


async def test_corepack_home_readable_and_populated_inside_sandbox(tmp_path: Path):
    """End-to-end against the real, potentially-populated corepack cache on
    whatever machine runs this test — skips cleanly where there is none."""
    real_cache = Path.home() / ".cache" / "node" / "corepack"
    if not real_cache.is_dir() or not any(real_cache.iterdir()):
        pytest.skip("no populated ~/.cache/node/corepack on this machine to verify against")
    result = await run_sandboxed(["sh", "-c", 'echo "$COREPACK_HOME" && ls "$COREPACK_HOME"'], tmp_path, timeout_s=10)
    assert result.exit_code == 0
    assert str(real_cache) in result.combined_tail


def test_generic_mounts_structurally_precede_every_specific_bind(tmp_path: Path):
    """Round-9 regression for the ordering bug class (not just the one /tmp
    case a test happened to catch): the three broad, low-specificity mounts
    (--proc /proc, --dev /dev, --tmpfs /tmp) must appear before every
    --ro-bind/--bind pair in the built argv, structurally — not merely by
    convention at each call site — since bwrap applies binds in order and a
    later bind silently remounts over an earlier one nested inside it.
    Credential-mask --tmpfs calls (for .ssh/.aws/etc, added deliberately
    *after* the broader binds they override) are a different, intentional
    case and are excluded from this check by their target path.
    """
    argv = build_sandboxed_argv(["true"], tmp_path, tmp_path / "scratch")

    def _flag_indices(flag: str, target: str | None = None) -> list[int]:
        indices = []
        i = 0
        while i < len(argv):
            if argv[i] == flag:
                if target is None or (i + 1 < len(argv) and argv[i + 1] == target):
                    indices.append(i)
            i += 1
        return indices

    generic_indices = (
        _flag_indices("--proc", "/proc") + _flag_indices("--dev", "/dev") + _flag_indices("--tmpfs", "/tmp")
    )
    assert generic_indices, "expected the three generic bootstrap mounts to be present"
    last_generic = max(generic_indices)

    bind_indices = _flag_indices("--ro-bind") + _flag_indices("--bind")
    assert bind_indices, "expected at least one specific bind"
    assert last_generic < min(bind_indices), (
        "a specific --ro-bind/--bind precedes the generic /proc,/dev,/tmp mounts — "
        "it would be silently wiped if its target ever nested under one of them"
    )


def test_refuses_to_sandbox_repo_equal_to_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ValueError, match="overlaps the real home directory"):
        build_sandboxed_argv(["true"], tmp_path, tmp_path / "scratch")


def test_refuses_to_sandbox_repo_that_is_an_ancestor_of_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(ValueError, match="overlaps the real home directory"):
        build_sandboxed_argv(["true"], tmp_path, tmp_path / "scratch")


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


# -- round-10 regression: network-enabled sandbox cannot read the ------------
# -- orchestrator's own API even though it can reach 127.0.0.1 -------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0).status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError("backend never became healthy")


@pytest.mark.skipif(os.name != "posix", reason="spawns a real subprocess server")
def test_network_enabled_sandbox_cannot_read_orchestrator_api(tmp_path: Path):
    """Round-10 regression for the proven finding: allow_network=True puts the
    sandboxed process in the *host's* network namespace (bwrap has no
    per-destination firewall), so it can reach 127.0.0.1:<port> — including
    the orchestrator's own API, live for the whole install step. Proven
    exploitable pre-fix: an unauthenticated GET returned real cross-project
    data because GET routes were exempt from the bearer-token check. The fix
    was requiring the token on GET too (see api/auth.py's module docstring),
    not trying to firewall the sandbox's network access (bwrap can't).

    Reconstructs that exact proof through the real production pieces: a real
    spawned backend (real AuthMiddleware, real seeded project data) and the
    real `run_sandboxed(..., allow_network=True)` primitive `_install` uses —
    confirms the sandboxed process can still reach the port (network isn't
    blocked) but gets 401, not the seeded project's data.
    """
    backend_root = Path(__file__).resolve().parents[1]
    db = tmp_path / "server" / "srv.db"
    db.parent.mkdir(parents=True)
    port = _free_port()
    log = tmp_path / "server.log"
    venv_python = backend_root / ".venv" / "bin" / "python"
    binary = str(venv_python) if venv_python.exists() else sys.executable
    env = dict(os.environ, PYTHONPATH=str(backend_root / "src"))
    proc = subprocess.Popen(
        [
            binary,
            str(backend_root / "tests" / "helpers" / "isolated_server.py"),
            str(db),
            str(port),
            str(tmp_path / "home"),
        ],
        env=env,
        stdout=open(log, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _wait_health(port)
        token = (db.parent / "auth_token").read_text().strip()
        auth = {"Authorization": f"Bearer {token}"}
        seeded = tmp_path / "seeded-secret-project"
        seeded.mkdir()
        created = httpx.post(
            f"http://127.0.0.1:{port}/api/projects", json={"path": str(seeded)}, headers=auth, timeout=10
        )
        assert created.status_code in (200, 201), created.text

        repo = tmp_path / "install-sandbox-repo"
        repo.mkdir()
        probe = (
            "import json, urllib.request, pathlib\n"
            f"req = urllib.request.Request('http://127.0.0.1:{port}/api/projects')\n"
            "try:\n"
            "    with urllib.request.urlopen(req, timeout=5) as r:\n"
            "        status, body = r.status, r.read().decode()\n"
            "except urllib.error.HTTPError as e:\n"
            "    status, body = e.code, e.read().decode()\n"
            "pathlib.Path('result.json').write_text(json.dumps({'status': status, 'body': body}))\n"
        )
        (repo / "probe.py").write_text(probe)

        result = asyncio.run(run_sandboxed(["python3", "probe.py"], repo, timeout_s=15, allow_network=True))
        assert result.exit_code == 0, result.combined_tail

        captured = json.loads((repo / "result.json").read_text())
        # Reachable at all (proves this isn't accidentally passing because
        # the network really is blocked — allow_network genuinely allows
        # loopback, matching the reviewer's proof).
        assert captured["status"] != 0
        # ...but unauthorized, not real data.
        assert captured["status"] == 401, captured
        assert str(seeded) not in captured["body"]
        assert "detail" in json.loads(captured["body"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
