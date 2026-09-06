from orchestrator import git_ops
from orchestrator.review import parse_review_output


# === F-20: Large File Protection ===
async def test_f20_large_file_excluded_from_checkpoint(tmp_path):
    await git_ops.init_repo(tmp_path)
    (tmp_path / "small.txt").write_text("hello")
    (tmp_path / "large.bin").write_bytes(b"x" * (6 * 1024 * 1024))  # 6MB
    sha = await git_ops.checkpoint(tmp_path, "test checkpoint")
    assert sha  # small file committed
    out = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "small.txt" in out
    assert "large.bin" not in out  # large file excluded
    st = await git_ops.status(tmp_path)
    assert "large.bin" in st.untracked  # still untracked


async def test_f20_small_file_committed(tmp_path):
    await git_ops.init_repo(tmp_path)
    (tmp_path / "normal.py").write_text("print('ok')")
    sha = await git_ops.checkpoint(tmp_path, "commit small")
    assert sha
    out = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    assert "normal.py" in out


async def test_f20_threshold_override(tmp_path):
    await git_ops.init_repo(tmp_path)
    (tmp_path / "medium.bin").write_bytes(b"x" * (3 * 1024 * 1024))  # 3MB
    # With threshold of 2MB, should be excluded
    sha = await git_ops.checkpoint(tmp_path, "test", max_file_mb=2)
    if sha:
        out = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
        assert "medium.bin" not in out


# === Review Schema ===
def test_review_schema_rejects_bare_array():
    raw = "REVIEW_FINDINGS_JSON: [1, 2, 3]"
    parsed_ok, _findings = parse_review_output(raw)
    assert not parsed_ok, "Bare integer array should not be valid"


def test_review_schema_rejects_missing_severity():
    raw = 'REVIEW_FINDINGS_JSON: [{"description": "something wrong"}]'
    parsed_ok, _findings = parse_review_output(raw)
    assert not parsed_ok, "Missing severity should not be valid"


def test_review_schema_accepts_valid_finding():
    raw = (
        'REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "description": "bug",'
        ' "category": "correctness", "file": "a.py", "recommended_fix": "fix it"}]'
    )
    parsed_ok, findings = parse_review_output(raw)
    assert parsed_ok
    assert len(findings) == 1
    assert findings[0]["description"] == "bug"


def test_review_schema_empty_array_valid():
    raw = "REVIEW_FINDINGS_JSON: []"
    parsed_ok, findings = parse_review_output(raw)
    assert parsed_ok
    assert findings == []


def test_review_schema_rejects_invalid_category_type():
    raw = 'REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "description": "bug", "category": 123}]'
    parsed_ok, _findings = parse_review_output(raw)
    assert not parsed_ok, "Integer category should be rejected"


def test_review_schema_rejects_invalid_file_type():
    raw = 'REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "description": "bug", "file": 123}]'
    parsed_ok, _findings = parse_review_output(raw)
    assert not parsed_ok, "Integer file should be rejected"


def test_review_schema_rejects_invalid_recommended_fix_type():
    raw = 'REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "description": "bug", "recommended_fix": 123}]'
    parsed_ok, _findings = parse_review_output(raw)
    assert not parsed_ok, "Integer recommended_fix should be rejected"
