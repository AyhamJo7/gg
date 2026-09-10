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

Toolchain/cache directories under $HOME are bound by an explicit
allow-list of specific subpaths (not whole directories like `.cache`/
`.local`), because those two in particular are shared, general-purpose
dumping grounds for every application on the machine — a first version
of this module bound them wholesale and was proven, on a real
development machine, to expose a live Hugging Face API token and a
live Jupyter session-signing secret that had nothing to do with any
build toolchain. The allow-list is the actual boundary; the credential
file/dir masks below are defense in depth on top of it, not the
primary control.

bwrap is Linux-only. `sandbox_available()` must be checked by every
caller; there is deliberately no non-sandboxed fallback path for
planner-authored command execution.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from .process import ProcessResult, run_process

logger = logging.getLogger(__name__)

BWRAP = "bwrap"

# Specific, narrow subpaths under $HOME that legitimate verify/build
# commands may need read access to: toolchain binaries and package/module
# caches already populated by the separate, network-enabled install step.
# Deliberately NOT the whole ".cache"/".local" tree — see module docstring.
_HOME_TOOLCHAIN_SUBPATHS = (
    ".nvm",
    ".local/bin",
    ".local/share/uv",
    ".cargo/bin",
    ".cargo/registry",
    ".rustup",
    "go/pkg/mod",
    ".npm",
    ".cache/uv",
    ".cache/pip",
    # corepack (bundled with Node, provisions pnpm/yarn on first use) caches
    # downloaded package managers here; without this bind and the COREPACK_HOME
    # re-point below, corepack sees an empty cache on every sandboxed run and
    # tries to fetch the pinned package manager over network — which verify-time
    # sandboxing deliberately doesn't have. Same bug class as CARGO_HOME/
    # RUSTUP_HOME/GOPATH below, just for a toolchain none of those rounds'
    # "legitimate usage" proofs happened to exercise (they tested plain `npm
    # test`, which doesn't need corepack's provisioning step at all).
    ".cache/node/corepack",
)

# Defense in depth on top of the allow-list above, in case a future
# addition to _HOME_TOOLCHAIN_SUBPATHS ever pulls in a directory that
# itself contains a credential file/dir under one of these names.
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
_SYSTEM_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")

# Environment variables explicitly forwarded into the sandbox despite
# --clearenv wiping everything else (including any credentials/tokens
# present in the orchestrator's own process environment). PATH is
# rebuilt (see _build_sandboxed_path), not forwarded verbatim.
_FORWARDED_ENV = ("LANG", "LC_ALL", "TERM")

# Cap on processes the sandboxed command tree may create, applied via the
# shell's ulimit (bwrap has no --rlimit flag). This is a coarse anti-forkbomb
# backstop, not a precise resource guarantee — a real cgroup-based pids.max
# would bound this per-sandbox rather than against the whole real UID; see
# module docstring / SECURITY.md for the tracked follow-up. Chosen well
# above any legitimate parallel test-runner's process count while still
# stopping runaway forking within a fraction of a second.
_MAX_SANDBOX_PROCS = 2048


def sandbox_available() -> bool:
    return shutil.which(BWRAP) is not None


def _bind_ro(bwrap_args: list[str], path: Path) -> None:
    if path.is_dir() or path.is_symlink():
        bwrap_args += ["--ro-bind", str(path), str(path)]


def _resolve_toolchain_binds(home: Path) -> list[Path]:
    resolved: list[Path] = []
    for rel in _HOME_TOOLCHAIN_SUBPATHS:
        p = home / rel
        if p.is_dir() or p.is_symlink():
            resolved.append(p)
    return resolved


def _build_sandboxed_path(toolchain_dirs: list[Path]) -> str:
    """Rebuild PATH from only directories actually reachable inside the
    sandbox, instead of forwarding the operator's full host PATH verbatim
    (which would name directories, e.g. version-manager or unrelated tool
    installs, that aren't bound and can only ever fail lookups there —
    unnecessary machine-specific coupling for zero functional benefit).

    `toolchain_dirs` must be toolchain-specific binds ONLY (nvm, cargo/bin,
    ...) — not the generic `_SYSTEM_RO_DIRS` (`/usr`, `/bin`, ...). Those
    are placed *before* the fixed `_SYSTEM_BIN_DIRS` fallback, preserving
    their relative order from the host PATH, so a project's pinned/
    version-managed toolchain wins over a same-named stray system package
    (e.g. an apt-installed `nodejs` sharing a machine with nvm). Passing a
    generic system directory in `toolchain_dirs` defeats this: a host PATH
    entry under `/usr` (present on essentially every machine, e.g.
    `/usr/bin` itself) would then compete for priority placement purely on
    host PATH order, silently reintroducing the shadowing bug this function
    exists to prevent — caught by a test that put `/usr/bin` before an nvm
    directory in a synthetic host PATH specifically to catch this.
    """
    toolchain_entries: list[str] = []
    host_path = os.environ.get("PATH", "")
    for raw in host_path.split(os.pathsep):
        if not raw:
            continue
        try:
            candidate = Path(raw).resolve()
        except OSError:
            continue
        if any(candidate == b or candidate.is_relative_to(b) for b in toolchain_dirs) and raw not in toolchain_entries:
            toolchain_entries.append(raw)
    entries = [*toolchain_entries, *(d for d in _SYSTEM_BIN_DIRS if d not in toolchain_entries)]
    return os.pathsep.join(entries)


def _guard_repo_not_home(repo: Path) -> None:
    """Reject a repo path that would let its own (later, read-write) bind
    remount over an earlier home-relative credential mask — see module
    docstring. Only the exact-match and repo-is-ancestor-of-home cases are
    dangerous; a repo nested under home (the normal case: ~/projects/x) is
    safe, since its mount point never overlaps a home-relative mask.
    """
    resolved_repo = repo.resolve()
    resolved_home = Path.home().resolve()
    if resolved_repo == resolved_home or resolved_repo in resolved_home.parents:
        raise ValueError(f"refusing to sandbox with repo={resolved_repo}: overlaps the real home directory boundary")


def build_sandboxed_argv(argv: list[str], repo: Path, scratch_home: Path, allow_network: bool = False) -> list[str]:
    """Wrap `argv` in a bwrap invocation confined to `repo` (read-write) plus a
    minimal read-only system/toolchain view. `scratch_home` is a fresh,
    per-invocation writable directory the caller creates and cleans up (see
    `run_sandboxed`); it becomes the sandboxed HOME.

    `allow_network=True` omits `--unshare-net` — the one confinement a
    dependency-install step cannot run without (it must reach a package
    registry) — while every other boundary (PID/IPC/UTS isolation,
    --clearenv, the credential-masked toolchain allow-list, no real-$HOME
    exposure, the ulimit forkbomb backstop) stays identical. This does not
    scope *which* network destinations are reachable, and the installed
    package's own content can still be exfiltrated over that network
    (inherent to needing network for install at all) — see SECURITY.md for
    the full honest statement of what this mode does and doesn't contain.
    """
    _guard_repo_not_home(repo)
    home = Path.home()
    bwrap_args: list[str] = [BWRAP, "--clearenv"]

    # Generic mount points come first, before ANY specific bind (including
    # the system dirs immediately below) — this is a structural guarantee,
    # not a convention to remember at each new bind site: /proc, /dev, and a
    # fresh empty /tmp are broad, low-specificity mounts, and bwrap applies
    # binds in argv order — a later bind completely remounts over an earlier
    # one nested inside it. Placing these three literally first means no
    # future specific bind, wherever it's added below, can ever be silently
    # wiped by them regardless of its target path. Getting this backwards is
    # silent and only bites when some later, more specific bind target
    # happens to be a subpath of /tmp (real $HOME never is, but this
    # ordering bug is worth being correct about on its own terms, not just
    # because it happened to be caught once by a test using a tmp_path-based
    # fixture home).
    bwrap_args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]  # noqa: S108 - sandbox mount point, not a real temp-file use

    for d in _SYSTEM_RO_DIRS:
        _bind_ro(bwrap_args, Path(d))

    toolchain_binds = _resolve_toolchain_binds(home)
    for p in toolchain_binds:
        _bind_ro(bwrap_args, p)

    # Mask credential-bearing paths *after* the broader binds above so they
    # take precedence at the same mount point (bwrap applies binds in order)
    # — defense in depth on top of the allow-list, see module docstring.
    for rel in _HOME_MASK_FILES:
        p = home / rel
        if p.is_file():
            bwrap_args += ["--ro-bind", "/dev/null", str(p)]
    for rel in _HOME_MASK_DIRS:
        p = home / rel
        if p.is_dir():
            bwrap_args += ["--tmpfs", str(p)]

    bwrap_args += ["--bind", str(scratch_home), str(scratch_home)]
    bwrap_args += ["--bind", str(repo), str(repo)]
    bwrap_args += ["--chdir", str(repo)]
    bwrap_args += [
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-pid",
        "--new-session",
        "--die-with-parent",
    ]
    if not allow_network:
        bwrap_args += ["--unshare-net"]

    for name in _FORWARDED_ENV:
        value = os.environ.get(name)
        if value is not None:
            bwrap_args += ["--setenv", name, value]
    bwrap_args += ["--setenv", "PATH", _build_sandboxed_path(toolchain_binds)]

    bwrap_args += ["--setenv", "HOME", str(scratch_home)]
    bwrap_args += ["--setenv", "TMPDIR", str(scratch_home)]
    bwrap_args += ["--setenv", "XDG_CACHE_HOME", str(scratch_home / "cache")]
    bwrap_args += ["--setenv", "npm_config_cache", str(scratch_home / "npm-cache")]
    bwrap_args += ["--setenv", "UV_CACHE_DIR", str(scratch_home / "uv-cache")]
    bwrap_args += ["--setenv", "PIP_CACHE_DIR", str(scratch_home / "pip-cache")]
    bwrap_args += ["--setenv", "GOCACHE", str(scratch_home / "go-cache")]
    bwrap_args += ["--setenv", "GOFLAGS", "-mod=mod"]
    bwrap_args += ["--setenv", "CARGO_TARGET_DIR", str(repo / "target")]
    # cargo/rustup/go resolve their state via $HOME/.cargo, $HOME/.rustup,
    # $HOME/go by default; HOME is overridden to the scratch dir above, so
    # each is re-pointed explicitly at the real (read-only-bound) toolchain
    # path rather than silently failing to find it under the scratch HOME —
    # this exact bug was caught for cargo/rustup by actually running a real
    # build; GOPATH's equivalent was found only by review since no `go`
    # toolchain exists in this development environment to verify it live.
    if (home / ".cargo").is_dir():
        bwrap_args += ["--setenv", "CARGO_HOME", str(home / ".cargo")]
    if (home / ".rustup").is_dir():
        bwrap_args += ["--setenv", "RUSTUP_HOME", str(home / ".rustup")]
    if (home / "go").is_dir():
        # GOPATH's module cache (pkg/mod) is read-only-bound above; GOPATH
        # itself must point at the real path for `go` to find it there.
        # GOCACHE (build output, not the module cache) stays in scratch.
        bwrap_args += ["--setenv", "GOPATH", str(home / "go")]
    else:
        bwrap_args += ["--setenv", "GOPATH", str(scratch_home / "go")]
    corepack_home = home / ".cache/node/corepack"
    if corepack_home.is_dir():
        # corepack.cjs reads process.env.COREPACK_HOME directly (verified
        # against the installed corepack's own source), falling back to a
        # HOME-derived default otherwise — same re-point pattern as
        # CARGO_HOME/RUSTUP_HOME/GOPATH above, for the same reason: HOME is
        # the empty scratch dir, so an unset COREPACK_HOME always looks empty.
        bwrap_args += ["--setenv", "COREPACK_HOME", str(corepack_home)]

    real_argv = argv
    bash = shutil.which("bash")
    if bash is not None:
        # bwrap has no --rlimit flag; cap process count via the shell's
        # ulimit as a coarse anti-forkbomb backstop (see module docstring).
        # /bin/sh on this platform (dash) doesn't support `ulimit -u` —
        # confirmed by testing; bash does.
        real_argv = [
            bash,
            "-c",
            f'ulimit -u {_MAX_SANDBOX_PROCS} 2>/dev/null; exec "$@"',
            "sandboxed-command",
            *argv,
        ]
    else:
        logger.warning("bash not found; sandboxed execution will not have a process-count limit applied")

    bwrap_args += ["--"]
    bwrap_args += real_argv
    return bwrap_args


async def run_sandboxed(
    argv: list[str],
    repo: Path,
    timeout_s: float,
    on_output: Callable[[str, str], None] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    cancel_event: asyncio.Event | None = None,
    allow_network: bool = False,
) -> ProcessResult:
    """Run `argv` confined to `repo` via bwrap. Caller must check
    `sandbox_available()` first — this raises if bwrap is missing rather
    than silently running unsandboxed.

    `allow_network=True` is for the dependency-install step only — see
    `build_sandboxed_argv`'s docstring for exactly what that does and does
    not contain. Every other caller should leave this False.
    """
    if not sandbox_available():
        raise RuntimeError("sandboxed execution unavailable: bubblewrap (bwrap) not found on this system")

    scratch = Path(tempfile.mkdtemp(prefix="gg-sandbox-"))
    try:
        for sub in ("cache", "npm-cache", "uv-cache", "pip-cache", "go", "go-cache"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        sandboxed_argv = build_sandboxed_argv(argv, repo, scratch, allow_network=allow_network)
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
