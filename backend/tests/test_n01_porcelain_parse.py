from pathlib import Path

import pytest

from orchestrator.git_ops import _git, init_repo, status


@pytest.mark.asyncio
async def test_git_status_porcelain_parsing(tmp_path: Path):
    await init_repo(tmp_path)
    
    # Setup initial commits to track files
    (tmp_path / "calc.py").write_text("print('hello')")
    (tmp_path / "staged.py").write_text("print('a')")
    (tmp_path / "del.py").write_text("print('del')")
    (tmp_path / "old_name.py").write_text("print('rename')")
    await _git(tmp_path, "add", ".")
    await _git(tmp_path, "commit", "-m", "init")

    # 1. Modify a tracked file (unstaged)
    (tmp_path / "calc.py").write_text("print('world')")
    
    # 2. Stage a modification
    (tmp_path / "staged.py").write_text("print('b')")
    await _git(tmp_path, "add", "staged.py")

    # 3. Add a new file
    (tmp_path / "new_file.py").write_text("print('new')")
    await _git(tmp_path, "add", "new_file.py")

    # 4. Delete a tracked file
    (tmp_path / "del.py").unlink()

    # 5. Untracked file
    (tmp_path / "untracked.py").write_text("print('untracked')")

    # 6. File with spaces in name
    (tmp_path / "space file.py").write_text("print('space')")
    await _git(tmp_path, "add", "space file.py")
    
    # 7. File with unicode characters
    (tmp_path / "ümlaut.py").write_text("print('unicode')")
    await _git(tmp_path, "add", "ümlaut.py")

    # 8. File with leading dash
    (tmp_path / "-leading-dash.py").write_text("print('dash')")
    await _git(tmp_path, "add", "--", "-leading-dash.py")

    # 9. Rename a file using git mv
    await _git(tmp_path, "mv", "old_name.py", "new_name.py")

    # 10. Call status
    st = await status(tmp_path)

    # Asserts exact filenames
    assert "calc.py" in st.modified
    assert "staged.py" in st.modified
    assert "new_file.py" in st.added
    assert "del.py" in st.deleted
    assert "untracked.py" in st.untracked
    assert "space file.py" in st.added
    assert "ümlaut.py" in st.added
    assert "-leading-dash.py" in st.added
    
    assert ("old_name.py", "new_name.py") in st.renamed
