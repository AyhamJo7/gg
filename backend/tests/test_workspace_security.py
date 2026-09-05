"""F-002 regression suite: workspace allow-roots policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator.security import default_allowed_roots, validate_workspace_path


class TestDefaultPolicy:
    def test_allowed_project_under_tmp(self, tmp_path: Path):
        project = tmp_path / "my-project"
        project.mkdir()
        assert validate_workspace_path(project) == project.resolve()

    def test_allowed_nested_project(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert validate_workspace_path(nested) == nested.resolve()

    def test_path_with_spaces(self, tmp_path: Path):
        spaced = tmp_path / "my project with spaces"
        spaced.mkdir()
        assert validate_workspace_path(spaced) == spaced.resolve()

    def test_nonexistent_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="does not exist"):
            validate_workspace_path(tmp_path / "missing")

    def test_file_rejected(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            validate_workspace_path(f)

    @pytest.mark.parametrize(
        "system_path",
        ["/", "/etc", "/usr", "/usr/local", "/var", "/bin", "/sbin", "/etc/nginx"],
    )
    def test_system_trees_rejected(self, system_path: str):
        with pytest.raises(ValueError):
            validate_workspace_path(system_path)

    def test_home_dir_itself_rejected(self):
        with pytest.raises(ValueError, match="root itself"):
            validate_workspace_path(Path.home())

    def test_tmp_itself_rejected(self):
        with pytest.raises(ValueError, match="root itself"):
            validate_workspace_path("/tmp")

    def test_traversal_rejected(self, tmp_path: Path):
        # /tmp/x/../../etc resolves to /etc
        with pytest.raises(ValueError):
            validate_workspace_path(str(tmp_path / ".." / ".." / "etc"))

    def test_symlink_to_allowed_target_accepted(self, tmp_path: Path):
        target = tmp_path / "real-project"
        target.mkdir()
        link = tmp_path / "link-project"
        link.symlink_to(target)
        assert validate_workspace_path(link) == target.resolve()

    def test_symlink_escape_to_etc_rejected(self, tmp_path: Path):
        link = tmp_path / "evil-link"
        link.symlink_to("/etc")
        with pytest.raises(ValueError):
            validate_workspace_path(link)

    def test_symlink_to_home_subdir_accepted(self, tmp_path: Path):
        home_project = Path.home() / ".gg-test-ws-link-target"
        try:
            home_project.mkdir(exist_ok=True)
            link = tmp_path / "home-link"
            link.symlink_to(home_project)
            assert validate_workspace_path(link) == home_project.resolve()
        finally:
            home_project.rmdir()

    def test_sensitive_dot_directories_rejected(self, tmp_path: Path):
        for sensitive in [".ssh", ".gnupg", ".aws", ".config", ".local", ".claude"]:
            sensitive_dir = tmp_path / sensitive
            sensitive_dir.mkdir()
            with pytest.raises(ValueError, match="Refusing sensitive directory as workspace"):
                validate_workspace_path(sensitive_dir)

    def test_validation_error_preserves_input_path(self):
        input_path = "some/relative/nonexistent/path"
        with pytest.raises(ValueError) as exc:
            validate_workspace_path(input_path)
        assert input_path in str(exc.value)

    def test_default_roots_are_general(self):
        roots = default_allowed_roots()
        assert Path.home() in roots
        assert Path("/tmp") in roots


class TestConfiguredRoots:
    def test_custom_roots_enforced(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        project = allowed / "repo"
        project.mkdir()
        assert validate_workspace_path(project, [allowed]) == project.resolve()
        with pytest.raises(ValueError, match="outside allowed roots"):
            validate_workspace_path(other, [allowed])

    def test_configured_root_itself_rejected(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        with pytest.raises(ValueError, match="root itself"):
            validate_workspace_path(allowed, [allowed])

    def test_symlink_inside_allowed_root_to_outside_rejected(self, tmp_path: Path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = allowed / "escape"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="outside allowed roots"):
            validate_workspace_path(link, [allowed])

    def test_tilde_expansion_in_roots(self, tmp_path: Path):
        home_project = Path.home() / ".gg-test-configured"
        try:
            home_project.mkdir(exist_ok=True)
            assert validate_workspace_path(home_project, [Path("~")]) == home_project.resolve()
        finally:
            home_project.rmdir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
class TestSystemReality:
    def test_etc_passwd_parent_rejected(self):
        with pytest.raises(ValueError):
            validate_workspace_path("/etc")

    def test_var_log_rejected(self):
        with pytest.raises(ValueError):
            validate_workspace_path("/var/log")
