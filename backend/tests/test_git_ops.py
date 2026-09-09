import pytest

from orchestrator import git_ops


async def test_init_status_checkpoint(tmp_path):
    await git_ops.init_repo(tmp_path)
    assert await git_ops.is_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("hi")
    st = await git_ops.status(tmp_path)
    assert "hello.txt" in st.untracked
    sha = await git_ops.checkpoint(tmp_path, "test: first checkpoint")
    assert sha
    st = await git_ops.status(tmp_path)
    assert st.is_clean
    commits = await git_ops.recent_commits(tmp_path)
    assert any("first checkpoint" in c for c in commits)


async def test_checkpoint_excludes_secrets(tmp_path):
    await git_ops.init_repo(tmp_path)
    (tmp_path / ".env").write_text("SECRET=hunter2")
    (tmp_path / "app.py").write_text("print('x')")
    sha = await git_ops.checkpoint(tmp_path, "test: checkpoint")
    assert sha
    out = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "app.py" in out
    assert ".env" not in out
    # .env remains untracked, never committed
    st = await git_ops.status(tmp_path)
    assert ".env" in st.untracked


async def test_checkpoint_excludes_secret_content_regardless_of_filename(tmp_path):
    """Content-based scan is the independent second layer against a
    credential read anywhere in the pipeline (e.g. a sandbox allow-list
    gap) being laundered into permanent git history via an innocuous
    filename — the filename-pattern check above only catches known-bad
    *names*, not secret-shaped *content* under an unremarkable name."""
    await git_ops.init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hf_AmUxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    (tmp_path / "app.py").write_text("print('x')")
    sha = await git_ops.checkpoint(tmp_path, "test: checkpoint")
    assert sha
    out = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "app.py" in out
    assert "README.md" not in out
    st = await git_ops.status(tmp_path)
    assert "README.md" in st.untracked


async def test_checkpoint_no_changes_returns_none(tmp_path):
    await git_ops.init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    assert await git_ops.checkpoint(tmp_path, "test: one")
    assert await git_ops.checkpoint(tmp_path, "test: nothing new") is None


async def test_not_a_repo(tmp_path):
    assert not await git_ops.is_repo(tmp_path)
    st = await git_ops.status(tmp_path)
    assert not st.is_repo
    with pytest.raises(git_ops.GitError):
        await git_ops.checkpoint(tmp_path, "nope")


async def test_f01_git_command_contract_and_spawn_git(tmp_path):
    # Non-repo check must return False without AttributeError
    assert not await git_ops.is_repo(tmp_path)
    assert await git_ops.head_sha(tmp_path) is None

    # Spawn git directly and verify GitCommandResult contract
    res = await git_ops._spawn_git(tmp_path, "status")
    assert isinstance(res, git_ops.GitCommandResult)
    assert isinstance(res.returncode, int)
    assert isinstance(res.stdout, bytes)
    assert isinstance(res.stderr, bytes)

    # Now init repo and verify operations
    await git_ops.init_repo(tmp_path)
    assert await git_ops.is_repo(tmp_path)
    (tmp_path / "f.txt").write_text("hello")
    sha = await git_ops.checkpoint(tmp_path, "commit 1")
    assert sha
    assert await git_ops.head_sha(tmp_path) == sha
