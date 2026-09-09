"""Unit tests for the criterion/gate-validation command allowlist.

Regression coverage for the RCE hardening: inline-code interpreter flags and
substring-matched blocked tokens used to slip through (see project history).
"""

from __future__ import annotations

from orchestrator.criterion import is_executable_command


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
    # Regression: the old substring-based denylist rejected any command
    # merely containing "git" as a substring anywhere in the text.
    ok, command = is_executable_command("npm run configure-git-hooks")
    assert ok is True
    assert command == "npm run configure-git-hooks"


def test_npm_publish_sequence_rejected():
    ok, _ = is_executable_command("npm publish --access public")
    assert ok is False


def test_git_as_first_word_still_rejected():
    ok, _ = is_executable_command("git push origin main")
    assert ok is False
