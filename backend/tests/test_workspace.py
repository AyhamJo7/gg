from orchestrator.workspace import inspect_workspace


async def test_detect_node_project(workspace):
    info = await inspect_workspace(workspace)
    assert info.project_type == "node"
    assert "npm" in info.package_managers
    assert any("test" in c for c in info.test_commands)
    assert any("build" in c for c in info.build_commands)
    assert info.readme == "README.md"


async def test_detect_python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    (tmp_path / "AGENTS.md").write_text("# rules")
    info = await inspect_workspace(tmp_path)
    assert info.project_type == "python"
    assert "AGENTS.md" in info.instruction_files
    assert any("pytest" in c for c in info.test_commands)


async def test_unknown_project(tmp_path):
    info = await inspect_workspace(tmp_path)
    assert info.project_type == "unknown"
    assert info.test_commands == []
