"""Usage parser contracts: provenance, completeness, no double-counting."""

from __future__ import annotations

import json

from orchestrator import usage as U


def _line(obj: dict) -> str:
    return json.dumps(obj)


def test_claude_full_final_usage():
    lines = [
        _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        _line(
            {
                "type": "result",
                "subtype": "success",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 92,
                    "output_tokens": 22763,
                    "cache_creation_input_tokens": 113923,
                    "cache_read_input_tokens": 4549613,
                },
            }
        ),
    ]
    summary = U.parse_claude_usage(lines)
    assert summary.source == "PROVIDER_REPORTED"
    assert summary.completeness == "COMPLETE"
    assert summary.input_tokens_total == 92 + 113923 + 4549613
    assert summary.output_tokens_total == 22763
    assert summary.cache_read_input_tokens == 4549613
    assert summary.cache_write_input_tokens == 113923
    assert summary.observed_model == "claude-sonnet-5"
    assert summary.input_basis == "FINAL_INVOCATION"


def test_claude_missing_final_usage_is_unknown():
    lines = [
        _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "more"}]}}),
    ]
    summary = U.parse_claude_usage(lines)
    assert summary.input_tokens_total is None
    assert summary.output_tokens_total is None
    assert summary.completeness == "UNKNOWN"


def test_claude_duplicate_assistant_not_summed():
    # Two identical assistant snapshots + one final result: total comes from
    # the final result only, never assistant sum + result.
    assistant = _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}})
    lines = [
        assistant,
        assistant,
        _line({"type": "result", "subtype": "success", "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]
    summary = U.parse_claude_usage(lines)
    assert summary.input_tokens_total == 10
    assert summary.output_tokens_total == 5


def test_opencode_step_totals_with_dedup():
    def step(sid: str, tokens: dict) -> str:
        return _line({"type": "step_finish", "sessionID": "s1", "part": {"id": sid, "type": "text", "tokens": tokens}})

    t1 = {"input": 100, "output": 50, "reasoning": 10, "cache": {"read": 200, "write": 30}, "total": 390}
    t2 = {"input": 70, "output": 20, "reasoning": 5, "cache": {"read": 100, "write": 10}, "total": 205}
    lines = [step("a", t1), step("b", t2), step("a", t1)]  # duplicate a
    summary = U.parse_opencode_usage(lines)
    assert summary.source == "CLI_REPORTED"
    assert summary.completeness == "COMPLETE"
    assert summary.input_tokens_total == 170
    assert summary.cache_read_input_tokens == 300
    assert summary.cache_write_input_tokens == 40
    assert summary.reasoning_output_tokens == 15
    # output normalized includes reasoning per observed fixture semantics
    assert summary.output_tokens_total == (50 + 20) + 15
    assert summary.native_total_tokens == 595
    assert summary.observations_count == 2


def test_opencode_incomplete_is_partial():
    lines = [
        _line({"type": "step_start", "sessionID": "s1", "part": {"id": "a", "type": "step-start"}}),
        _line(
            {
                "type": "step_finish",
                "sessionID": "s1",
                "part": {"id": "a", "type": "text", "tokens": {"input": 10, "output": 5, "total": 15}},
            }
        ),
        _line({"type": "step_start", "sessionID": "s1", "part": {"id": "b", "type": "step-start"}}),
    ]
    summary = U.parse_opencode_usage(lines)
    # Started step b has no finish: lower-bound, never complete.
    assert summary.completeness == "PARTIAL"
    assert summary.input_tokens_total == 10


def test_opencode_no_steps_is_unknown():
    summary = U.parse_opencode_usage(["hello world", "not json"])
    assert summary.source == "UNKNOWN"
    assert summary.input_tokens_total is None


def test_codex_cumulative_not_summed():
    snaps = [
        {"token_count": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}, "model": "gpt-5.6-sol"},
        {"token_count": {"input_tokens": 200, "output_tokens": 25, "total_tokens": 225}, "model": "gpt-5.6-sol"},
        {"token_count": {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330}, "model": "gpt-5.6-sol"},
    ]
    summary = U.parse_codex_cumulative_snapshot(snaps, attributable=True)
    # Final snapshot wins; never 100+200+300.
    assert summary.input_tokens_total == 300
    assert summary.output_tokens_total == 30
    assert summary.native_total_tokens == 330
    assert summary.input_basis == "CUMULATIVE_SESSION_SNAPSHOT"
    assert summary.observations_count == 3


def test_codex_unattributable_is_unknown():
    snaps = [{"token_count": {"input_tokens": 9999, "output_tokens": 999, "total_tokens": 10998}}]
    summary = U.parse_codex_cumulative_snapshot(snaps, attributable=False)
    assert summary.source == "UNKNOWN"
    assert summary.input_tokens_total is None


def test_codex_stdout_without_usage_is_unknown():
    lines = [
        _line({"type": "item.completed", "item": {"text": "did a tool"}}),
        _line({"type": "agent_message", "message": "hello"}),
    ]
    summary = U.parse_codex_stdout_usage(lines)
    assert summary.source == "UNKNOWN"
    assert summary.input_tokens_total is None


def test_agy_non_result_lines_stay_unknown():
    lines = [_line({"type": "result", "result": {"tokens": 12345}})]
    summary = U.parse_agy_usage(lines)
    assert summary.source == "UNKNOWN"
    assert summary.input_tokens_total is None
    assert U.parse_usage_for_provider("agy", lines).source == "UNKNOWN"


AGY_DOGFOOD_RESULT = {
    "event": "result",
    "result": {
        "status": "SUCCESS",
        "response": "REVIEW_FINDINGS_JSON: []",
        "duration_seconds": 423.166498542,
        "num_turns": 1,
        "usage": {
            "input_tokens": 416729,
            "output_tokens": 32542,
            "thinking_tokens": 23929,
            "cache_read_tokens": 4885861,
            "total_tokens": 449271,
        },
    },
}


def test_agy_parses_terminal_result_usage_from_dogfood_log():
    summary = U.parse_agy_usage([_line({"event": "progress"}), _line(AGY_DOGFOOD_RESULT)])
    assert summary.source == "PROVIDER_REPORTED"
    assert summary.input_tokens_total == 416729 + 4885861
    assert summary.output_tokens_total == 32542
    assert summary.reasoning_output_tokens == 23929
    assert summary.native_total_tokens == 449271
    # Thinking/output relation is unstated: never claim completeness.
    assert summary.completeness == "PARTIAL"
    assert summary.evidence_kind == "agy:result.usage"


def test_agy_result_without_usage_stays_unknown():
    summary = U.parse_agy_usage([_line({"event": "result", "result": {"status": "SUCCESS"}})])
    assert summary.source == "UNKNOWN"
    assert summary.evidence_kind == "agy:result-without-usage"


def test_agy_missing_fields_stay_none_not_zero():
    summary = U.parse_agy_usage([_line({"event": "result", "result": {"usage": {"output_tokens": 5}}})])
    assert summary.input_tokens_total is None
    assert summary.output_tokens_total == 5
    # Cache reads alone are not an input total.
    only_cache = U.parse_agy_usage(
        [_line({"event": "result", "result": {"usage": {"cache_read_tokens": 9, "note": "x" * 999}}})]
    )
    assert only_cache.input_tokens_total is None
    assert only_cache.native_counts == {"usage": {"cache_read_tokens": 9}}
