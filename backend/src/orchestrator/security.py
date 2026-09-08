"""Security helpers: path validation and secret redaction.

Provider CLIs are privileged processes. All workspace paths are canonicalized
and validated before use; all log output passes through redaction.
"""

from __future__ import annotations

import re
from pathlib import Path

# Common secret shapes. Keep conservative: redact on suspicion.
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GH_PAT]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^\s'\"]{8,}"), r"\1=[REDACTED]"),
]

# Filenames that must never be committed by the git ledger.
SENSITIVE_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.env(\..*)?$"),
    re.compile(r"(^|/)\.env\.local$"),
    re.compile(r".*\.(pem|key|p12|pfx|ppk|jks|keystore)$", re.IGNORECASE),
    re.compile(r"(^|/)id_(rsa|ed25519|ecdsa|dsa).*"),
    re.compile(r"(^|/)\.(npmrc|pypirc|netrc|git-credentials)$"),
    re.compile(r"(^|/)[^/]*credentials[^/]*$", re.IGNORECASE),
    re.compile(r"(^|/)auth\.json$", re.IGNORECASE),
    re.compile(r"(^|/)[^/]*secret[^/]*\.ya?ml$", re.IGNORECASE),
    re.compile(r"(^|/)(\.aws|\.ssh|\.orchestrator)(/|$)"),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# Upper bound for the redaction context overlap (MED-01). When serving a
# bounded tail of a log file, patterns whose anchor (e.g. a YAML key) sits
# before the cut point would otherwise miss their value. We therefore read
# up to TAIL_OVERLAP_MAX extra bytes, redact the whole window together, and
# only then drop everything before the cut — so every served character
# passed through redact() with its full surrounding context present.
# Residual assumption (documented, bounded): an anchor separated from its
# value by more than the overlap is out of context and will not link.
TAIL_OVERLAP_MAX = 65536


def read_redacted_tail(path: Path, cap_bytes: int) -> tuple[str, int, bool]:
    """Read at most cap_bytes from the end of path, redacted.

    Returns (served_text, file_size_bytes, truncated). Served text consists
    of whole redacted lines only — redacted lines are never split, so no
    partial credential fragment can leak at a truncation boundary. Reads
    are bounded to cap_bytes + overlap regardless of file size.
    """
    cap = max(1024, min(cap_bytes, 1048576))
    size = path.stat().st_size
    truncated = size > cap
    overlap = min(cap, TAIL_OVERLAP_MAX)
    start = max(0, size - cap - overlap) if truncated else 0
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    redacted = redact(text)
    if not truncated:
        return redacted, size, False
    lines = redacted.split("\n")
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        total += len(line.encode("utf-8")) + 1
        if total > cap and kept:
            break
        kept.append(line)
    served = "\n".join(reversed(kept))
    if len(served.encode("utf-8")) > cap:
        # Pathological single line larger than the whole budget: keep tail
        # semantics, never serve more than the cap.
        served = served.encode("utf-8")[-cap:].decode("utf-8", errors="replace")
    return served, size, True


NEVER_SENSITIVE = {".env.example", ".env.sample", ".env.template"}


def is_sensitive_file(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip("/")
    basename = normalized.rsplit("/", 1)[-1]
    if basename in NEVER_SENSITIVE:
        return False
    return any(p.search(normalized) for p in SENSITIVE_FILE_PATTERNS)


# System trees that must never become AI workspaces, even if a caller
# manages to construct a path under an allowed root that reaches them.
SYSTEM_TREE_PREFIXES = (
    "/etc", "/usr", "/var", "/bin", "/sbin", "/boot", "/dev",
    "/proc", "/sys", "/lib", "/lib64", "/opt", "/snap", "/run",
)

# Sensitive directory names that must never become AI workspaces (F-16).
DENIED_WORKSPACE_PARTS = {
    ".ssh", ".gnupg", ".aws", ".config", ".local", ".claude",
}


def default_allowed_roots() -> list[Path]:
    """Default workspace policy: the user's home directory and /tmp.

    Projects must be strictly INSIDE one of these roots (the root itself is
    never a valid workspace). This is deliberately general — personal
    directory layouts belong in config (`security.allowed_roots`).
    """
    return [Path.home(), Path("/tmp")]  # noqa: S108


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def validate_workspace_path(path: str | Path, allowed_roots: list[Path] | None = None) -> Path:
    """Resolve and validate a user-supplied workspace path.

    Policy (allow-roots model):
    - the path is canonicalized with resolve() FIRST, so symlinks and `..`
      are evaluated against their real target;
    - the resolved path must be strictly inside one of the allowed roots
      (default: $HOME and /tmp — see default_allowed_roots);
    - system trees (/etc, /usr, /var, …) are always rejected;
    - sensitive dotfile directories (.ssh, .gnupg, .aws, …) are always rejected;
    - the allowed root itself (e.g. $HOME or /tmp) is never a valid workspace.

    Raises ValueError with a human-readable reason on rejection.
    """
    roots = allowed_roots if allowed_roots else default_allowed_roots()
    resolved_roots = [r.expanduser().resolve() for r in roots]
    p = Path(path).expanduser().resolve()  # follows symlinks, collapses ..
    if not p.exists():
        raise ValueError(f"Workspace path does not exist: {path}")
    if not p.is_dir():
        raise ValueError(f"Workspace path is not a directory: {path}")
    for prefix in SYSTEM_TREE_PREFIXES:
        if p == Path(prefix) or str(p).startswith(prefix + "/"):
            raise ValueError(f"Refusing system directory as workspace: {path}")
    for part in p.parts:
        if part in DENIED_WORKSPACE_PARTS:
            raise ValueError(f"Refusing sensitive directory as workspace: {path}")
    for root in resolved_roots:
        if p == root:
            raise ValueError(f"Refusing workspace at allowed root itself: {path} (choose a subdirectory)")
        if _is_within(p, root):
            return p
    roots_str = ", ".join(str(r) for r in resolved_roots)
    raise ValueError(f"Workspace path {path} is outside allowed roots ({roots_str})")


def ensure_within(root: Path, candidate: str | Path) -> Path:
    """Resolve candidate and guarantee it stays inside root (anti path-traversal)."""
    resolved = (root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError(f"Path escapes workspace: {candidate}")
    return resolved


GITIGNORE_SECRET_ENTRIES = [
    ".env",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "*credentials*",
    "*secret*.yaml",
    "*secret*.yml",
    "*.jks",
    "*.keystore",
    ".aws/",
    ".ssh/",
    ".orchestrator/",
    "node_modules/",
    "__pycache__/",
    "*.pyc",
    "dist/",
    "build/",
    ".venv/",
    "target/",
]


def ensure_gitignore_protections(git_root: Path) -> list[str]:
    """Append secret patterns to .gitignore if missing. Returns added entries."""
    gitignore = git_root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    existing_set = {line.strip() for line in existing}
    added: list[str] = []
    for entry in GITIGNORE_SECRET_ENTRIES:
        if entry not in existing_set:
            added.append(entry)
    if added:
        with gitignore.open("a") as fh:
            if existing and not existing[-1] == "":
                fh.write("\n")
            fh.write("# Added by GG Orchestrator — secret protection\n")
            fh.write("\n".join(added) + "\n")
    return added
