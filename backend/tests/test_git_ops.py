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
