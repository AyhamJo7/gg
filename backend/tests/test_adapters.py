"""Adapter command construction + output normalization (no real execution)."""

from pathlib import Path

from orchestrator.providers import (
    AgyAdapter,
    ClaudeAdapter,
    CodexAdapter,
    OpencodeAdapter,
)
from orchestrator.providers.base import ExecutionRequest


def req() -> ExecutionRequest:
    return ExecutionRequest(
        prompt="do the thing",
        workdir=Path("/tmp/ws"),
        role="implementation",
        timeout_s=120,
        run_id="r1",
        log_dir=Path("/tmp/logs"),
    )


def test_claude_command_shape():
    argv = ClaudeAdapter().build_command(req())
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "stream-json" in argv
    assert "bypassPermissions" in argv
    # prompt is a single argv element — no shell interpolation possible
    assert "do the thing" in argv


def test_codex_command_shape():
    argv = CodexAdapter().build_command(req())
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert "workspace-write" in argv
    assert "/tmp/ws" in argv


def test_agy_command_shape():
    argv = AgyAdapter().build_command(req())
    assert argv[0] == "agy"
    assert "--print" in argv
    assert "--dangerously-skip-permissions" in argv
    assert "2m" in argv  # timeout propagated


def test_opencode_command_shape():
    argv = OpencodeAdapter().build_command(req())
    assert argv[:2] == ["opencode", "run"]
    assert "--auto" in argv
    assert "/tmp/ws" in argv


def test_claude_normalizes_stream_json():
    adapter = ClaudeAdapter()
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello world"}]}}'
    assert adapter.normalize_output_line(line) == "hello world"
    assert adapter.normalize_output_line('{"type":"system","subtype":"init"}') is None
    assert adapter.normalize_output_line("plain text") == "plain text"


def test_opencode_normalizes_part_events():
    adapter = OpencodeAdapter()
    line = '{"type":"text","part":{"type":"text","text":"working on it"}}'
    assert adapter.normalize_output_line(line) == "working on it"


def test_codex_normalizes_agent_message():
    adapter = CodexAdapter()
    line = '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
    assert adapter.normalize_output_line(line) == "done"


def test_uninstalled_provider_detects_gracefully():
    adapter = ClaudeAdapter(executable="definitely-not-a-real-binary-xyz")
    installed, path = adapter.detect()
    assert not installed
    assert path is None
