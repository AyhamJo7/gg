"""Review normalization: JSON-streaming provider output → canonical assistant_text → parsed findings.

These tests use deterministic fixtures representing real provider NDJSON output.
"""

from __future__ import annotations

import json

from orchestrator.providers.agy import AgyAdapter
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.claude import ClaudeAdapter
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.opencode import OpencodeAdapter
from orchestrator.review import parse_review_output


def _claude_ndjson(lines: list[str]) -> str:
    return "\n".join(json.dumps(obj, ensure_ascii=False) for obj in lines)


def _opencode_events(texts: list[str]) -> str:
    lines = []
    for t in texts:
        lines.append(json.dumps({"type": "text", "part": {"type": "text", "text": t}}, ensure_ascii=False))
    lines.append(json.dumps({"type": "step_finish", "part": {"type": "finish"}}))
    return "\n".join(lines)


def _agy_events(texts: list[str]) -> str:
    lines = []
    for t in texts:
        lines.append(json.dumps({"event": "step_update", "step_update": {"text_delta": t}}, ensure_ascii=False))
    lines.append(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))
    return "\n".join(lines)


def _codex_events(texts: list[str]) -> str:
    lines = []
    for t in texts:
        lines.append(json.dumps({"type": "agent_message", "item": {"text": t}}, ensure_ascii=False))
    return "\n".join(lines)


# ===========================================================================
# Claude normalization
# ===========================================================================


def test_claude_extract_assistant_text_from_ndjson():
    adapter = ClaudeAdapter()
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "world"}]}},
        {"type": "result", "result": "Done"},
    ]
    stdout = _claude_ndjson(events).splitlines()
    text = adapter.extract_assistant_text(stdout)
    assert "Hello" in text
    assert "world" in text
    assert "Done" in text


def test_claude_review_markers_extracted():
    adapter = ClaudeAdapter()
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "REVIEW_FINDINGS_JSON: [{\"severity\":\"HIGH\",\"description\":\"bug\"}]"}]}},
    ]
    stdout = _claude_ndjson(events).splitlines()
    text = adapter.extract_assistant_text(stdout)
    ok, findings = parse_review_output(text)
    assert ok
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"


# ===========================================================================
# OpenCode normalization
# ===========================================================================


def test_opencode_extract_assistant_text():
    adapter = OpencodeAdapter()
    stdout = _opencode_events(["Hello ", "world"]).splitlines()
    text = adapter.extract_assistant_text(stdout)
    assert "Hello " in text
    assert "world" in text


def test_opencode_review_markers_extracted():
    adapter = OpencodeAdapter()
    stdout = _opencode_events(["REVIEW_FINDINGS_JSON: [{\"severity\":\"LOW\",\"description\":\"ok\"}]"]).splitlines()
    text = adapter.extract_assistant_text(stdout)
    ok, findings = parse_review_output(text)
    assert ok
    assert findings[0]["severity"] == "LOW"


# ===========================================================================
# AGY normalization
# ===========================================================================


def test_agy_extract_assistant_text():
    adapter = AgyAdapter()
    stdout = _agy_events(["Hello ", "world"]).splitlines()
    text = adapter.extract_assistant_text(stdout)
    assert "Hello " in text
    assert "world" in text


def test_agy_review_markers_extracted():
    adapter = AgyAdapter()
    stdout = _agy_events(["REVIEW_FINDINGS_JSON: [{\"severity\":\"BLOCKER\",\"description\":\"crash\"}]"]).splitlines()
    text = adapter.extract_assistant_text(stdout)
    ok, findings = parse_review_output(text)
    assert ok
    assert findings[0]["severity"] == "BLOCKER"


# ===========================================================================
# Codex normalization
# ===========================================================================


def test_codex_extract_assistant_text():
    adapter = CodexAdapter()
    stdout = _codex_events(["Hello", "world"]).splitlines()
    text = adapter.extract_assistant_text(stdout)
    assert "Hello" in text
    assert "world" in text


def test_codex_review_markers_extracted():
    adapter = CodexAdapter()
    stdout = _codex_events(["REVIEW_FINDINGS_JSON: [{\"severity\":\"MEDIUM\",\"description\":\"style\"}]"]).splitlines()
    text = adapter.extract_assistant_text(stdout)
    ok, findings = parse_review_output(text)
    assert ok
    assert findings[0]["severity"] == "MEDIUM"


# ===========================================================================
# Engine integration: assistant_text used for review
# ===========================================================================


def test_execution_result_assistant_text_field():
    result = ExecutionResult(
        state=__import__("orchestrator.models", fromlist=["ProviderState"]).ProviderState.COMPLETED,
        failure_class=__import__("orchestrator.models", fromlist=["FailureClass"]).FailureClass.NONE,
        exit_code=0,
        duration_s=1.0,
        summary="ok",
        assistant_text="REVIEW_FINDINGS_JSON: [{\"severity\":\"HIGH\",\"description\":\"bug\"}]",
    )
    ok, findings = parse_review_output(result.assistant_text + "\n" + result.summary)
    assert ok
    assert len(findings) == 1


# ===========================================================================
# Edge cases
# ===========================================================================


def test_mixed_telemetry_does_not_pollute_assistant_text():
    adapter = ClaudeAdapter()
    events = [
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "REVIEW_FINDINGS_JSON: []"}]}},
    ]
    stdout = _claude_ndjson(events).splitlines()
    text = adapter.extract_assistant_text(stdout)
    assert "thinking_tokens" not in text
    assert "rate_limit_event" not in text
    ok, findings = parse_review_output(text)
    assert ok
    assert findings == []


def test_truncated_json_line_ignored():
    adapter = ClaudeAdapter()
    stdout = [
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"',
        '}]}}',
    ]
    text = adapter.extract_assistant_text(stdout)
    # Truncated lines should be ignored gracefully
    assert "OK" not in text


def test_empty_provider_stream():
    adapter = OpencodeAdapter()
    text = adapter.extract_assistant_text([])
    assert text == ""
