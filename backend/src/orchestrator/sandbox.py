"""OS-level sandboxed execution for planner-authored verify/gate commands.

Six remediation rounds tried to prove a command was safe to run by
statically enumerating every way a target repo's own manifests
(go.mod/Cargo.toml/package.json) could redirect a build/test tool outside
the repo. Each round closed a proven bypass and a new, deeper one
appeared (argv flags, attached-flag disguises, regex-vs-real-grammar
mismatches, then manifest-schema fields nobody had enumerated yet:
workspaces, target-conditional dependency tables, workspace members,
go.work). Two independent adversarial reviews converged on the same
diagnosis: hand-enumerating "which manifest fields matter" has no
natural stopping point.

This module replaces that approach. Instead of proving a command is
safe before running it, the command runs for real inside a bubblewrap
(bwrap) sandbox that can only see the target repo (read-write), a
minimal read-only system/toolchain view, and no network. A manifest
directive that would have redirected the tool outside the repo now
fails at the kernel's mount-namespace boundary regardless of which
field caused it — the boundary doesn't depend on having enumerated the
mechanism.

bwrap is Linux-only. `sandbox_available()` must be checked by every
caller; there is deliberately no non-sandboxed fallback path for
planner-authored command execution.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from .process import ProcessResult, run_process

BWRAP = "bwrap"

# Toolchain-manager directories under $HOME that legitimate verify/build
# commands may need read access to (binaries, shared libraries, package/
# module caches already populated by the separate, network-enabled
# install step). Bound read-only. Credential-bearing paths that may live
# under any of these are masked out below regardless.
_HOME_TOOLCHAIN_DIRS = (".nvm", ".local", ".cargo", ".rustup", "go", ".npm", ".cache")

_HOME_MASK_FILES = (
    ".npmrc",
    ".netrc",
    ".git-credentials",
    ".gitconfig",
    ".cargo/credentials.toml",
    ".cargo/credentials",
)
_HOME_MASK_DIRS = (".ssh", ".aws", ".azure", ".gnupg", ".config/gh", ".docker")

_SYSTEM_RO_DIRS = ("/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc")

# Environment variables explicitly forwarded into the sandbox despite
# --clearenv wiping everything else (including any credentials/tokens
# present in the orchestrator's own process environment).
_FORWARDED_ENV = ("PATH", "LANG", "LC_ALL", "TERM")


def sandbox_available() -> bool:
    return shutil.which(BWRAP) is not None


def _bind_ro(bwrap_args: list[str], path: Path) -> None:
    if path.is_dir() or path.is_symlink():
        bwrap_args += ["--ro-bind", str(path), str(path)]


def build_sandboxed_argv(argv: list[str], repo: Path, scratch_home: Path) -> list[str]:
    """Wrap `argv` in a bwrap invocation confined to `repo` (read-write) plus a
    minimal read-only system/toolchain view, with no network. `scratch_home`
    is a fresh, per-invocation writable directory the caller creates and
    cleans up (see `run_sandboxed`); it becomes the sandboxed HOME.
    """
    home = Path.home()
    bwrap_args: list[str] = [BWRAP, "--clearenv"]

    for d in _SYSTEM_RO_DIRS:
        _bind_ro(bwrap_args, Path(d))

    for rel in _HOME_TOOLCHAIN_DIRS:
        _bind_ro(bwrap_args, home / rel)

    # Mask credential-bearing paths *after* the broader binds above so they
    # take precedence at the same mount point (bwrap applies binds in order).
    for rel in _HOME_MASK_FILES:
        p = home / rel
        if p.is_file():
            bwrap_args += ["--ro-bind", "/dev/null", str(p)]
    for rel in _HOME_MASK_DIRS:
        p = home / rel
        if p.is_dir():
            bwrap_args += ["--tmpfs", str(p)]

    bwrap_args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]  # noqa: S108 - sandbox mount point, not a real temp-file use
    bwrap_args += ["--bind", str(scratch_home), str(scratch_home)]
    bwrap_args += ["--bind", str(repo), str(repo)]
    bwrap_args += ["--chdir", str(repo)]
    bwrap_args += ["--unshare-net", "--unshare-uts", "--unshare-ipc", "--die-with-parent"]

    for name in _FORWARDED_ENV:
        value = os.environ.get(name)
        if value is not None:
            bwrap_args += ["--setenv", name, value]

    bwrap_args += ["--setenv", "HOME", str(scratch_home)]
    bwrap_args += ["--setenv", "TMPDIR", str(scratch_home)]
    bwrap_args += ["--setenv", "XDG_CACHE_HOME", str(scratch_home / "cache")]
    bwrap_args += ["--setenv", "npm_config_cache", str(scratch_home / "npm-cache")]
    bwrap_args += ["--setenv", "UV_CACHE_DIR", str(scratch_home / "uv-cache")]
    bwrap_args += ["--setenv", "PIP_CACHE_DIR", str(scratch_home / "pip-cache")]
    bwrap_args += ["--setenv", "GOPATH", str(scratch_home / "go")]
    bwrap_args += ["--setenv", "GOCACHE", str(scratch_home / "go-cache")]
    bwrap_args += ["--setenv", "GOFLAGS", "-mod=mod"]
    bwrap_args += ["--setenv", "CARGO_TARGET_DIR", str(repo / "target")]
    # cargo/rustup resolve their state via $HOME/.cargo, $HOME/.rustup by
    # default; HOME is overridden to the scratch dir above, so re-point
    # these explicitly at the real (read-only-bound) toolchain dirs rather
    # than silently failing to find them under the scratch HOME.
    if (home / ".cargo").is_dir():
        bwrap_args += ["--setenv", "CARGO_HOME", str(home / ".cargo")]
    if (home / ".rustup").is_dir():
        bwrap_args += ["--setenv", "RUSTUP_HOME", str(home / ".rustup")]
    bwrap_args += ["--"]
    bwrap_args += argv
    return bwrap_args


async def run_sandboxed(
    argv: list[str],
    repo: Path,
    timeout_s: float,
    on_output: Callable[[str, str], None] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    cancel_event: asyncio.Event | None = None,
) -> ProcessResult:
    """Run `argv` confined to `repo` via bwrap. Caller must check
    `sandbox_available()` first — this raises if bwrap is missing rather
    than silently running unsandboxed.
    """
    if not sandbox_available():
        raise RuntimeError("sandboxed execution unavailable: bubblewrap (bwrap) not found on this system")

    scratch = Path(tempfile.mkdtemp(prefix="gg-sandbox-"))
    try:
        for sub in ("cache", "npm-cache", "uv-cache", "pip-cache", "go", "go-cache"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        sandboxed_argv = build_sandboxed_argv(argv, repo, scratch)
        return await run_process(
            sandboxed_argv,
            cwd=repo,
            timeout_s=timeout_s,
            on_output=on_output,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cancel_event=cancel_event,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
