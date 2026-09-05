"""Workspace inspection: validate a project folder and detect its toolchain."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .security import validate_workspace_path

INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md", ".cursorrules", "CONVENTIONS.md"]


@dataclass
class WorkspaceInfo:
    path: Path
    is_git_repo: bool
    project_type: str = "unknown"  # python | node | rust | go | unknown
    package_managers: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    lint_commands: list[str] = field(default_factory=list)
    typecheck_commands: list[str] = field(default_factory=list)
    has_docker: bool = False
    is_monorepo: bool = False
    instruction_files: list[str] = field(default_factory=list)
    readme: str | None = None

    def summary(self) -> str:
        lines = [
            f"project_type: {self.project_type}",
            f"git_repo: {self.is_git_repo}",
            f"package_managers: {', '.join(self.package_managers) or 'none'}",
            f"test_commands: {'; '.join(self.test_commands) or 'none detected'}",
            f"build_commands: {'; '.join(self.build_commands) or 'none detected'}",
            f"lint_commands: {'; '.join(self.lint_commands) or 'none detected'}",
            f"typecheck_commands: {'; '.join(self.typecheck_commands) or 'none detected'}",
            f"docker: {self.has_docker}",
            f"monorepo: {self.is_monorepo}",
            f"instruction_files: {', '.join(self.instruction_files) or 'none'}",
        ]
        return "\n".join(lines)


async def inspect_workspace(path: str | Path) -> WorkspaceInfo:
    from . import git_ops  # local import to avoid cycle at module load

    root = validate_workspace_path(path)
    info = WorkspaceInfo(path=root, is_git_repo=await git_ops.is_repo(root))

    has_py = (root / "pyproject.toml").exists() or (root / "requirements.txt").exists()
    has_node = (root / "package.json").exists()
    has_rust = (root / "Cargo.toml").exists()
    has_go = (root / "go.mod").exists()
    types = [t for t, present in (("python", has_py), ("node", has_node), ("rust", has_rust), ("go",
        has_go)) if present]
    info.project_type = "+".join(types) if len(types) > 1 else (types[0] if types else "unknown")

    if has_py:
        if (root / "uv.lock").exists() or (root / "pyproject.toml").exists():
            info.package_managers.append("uv")
            info.test_commands.append("uv run pytest -q")
            info.lint_commands.append("uv run ruff check .")
            info.typecheck_commands.append("uv run mypy .")
        else:
            info.package_managers.append("pip")
            info.test_commands.append("python -m pytest -q")
    if has_node:
        import json

        pkg = json.loads((root / "package.json").read_text())
        scripts = pkg.get("scripts", {})
        pm = "pnpm" if (root / "pnpm-lock.yaml").exists() else "npm"
        info.package_managers.append(pm)
        runner = f"{pm} run" if pm == "pnpm" else "npm run"
        if "test" in scripts:
            info.test_commands.append(f"{runner} test")
        if "build" in scripts:
            info.build_commands.append(f"{runner} build")
        if "lint" in scripts:
            info.lint_commands.append(f"{runner} lint")
        if "typecheck" in scripts or (root / "tsconfig.json").exists():
            info.typecheck_commands.append(f"{runner} typecheck" if "typecheck" in scripts else "npx tsc --noEmit")
    if has_rust:
        info.package_managers.append("cargo")
        info.test_commands.append("cargo test")
        info.build_commands.append("cargo build")
        info.lint_commands.append("cargo clippy -- -D warnings")
    if has_go:
        info.package_managers.append("go")
        info.test_commands.append("go test ./...")
        info.build_commands.append("go build ./...")

    info.has_docker = (root / "Dockerfile").exists() or (root / "docker-compose.yml").exists()
    info.is_monorepo = (root / "pnpm-workspace.yaml").exists() or (
        (root / "package.json").exists() and '"workspaces"' in (root / "package.json").read_text()
    )
    info.instruction_files = [f for f in INSTRUCTION_FILES if (root / f).exists()]
    for name in ("README.md", "README"):
        if (root / name).exists():
            info.readme = name
            break
    return info
