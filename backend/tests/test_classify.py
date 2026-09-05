from orchestrator.models import FailureClass
from orchestrator.providers.classify import classify_output


def test_rate_limit_patterns():
    assert classify_output(1, "Error: 429 too many requests", timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT
    assert classify_output(0, "You've hit your usage limit, try again at 5pm", timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT


def test_quota_exhausted_patterns():
    assert classify_output(1, "resource_exhausted: quota exceeded", timed_out=False, cancelled=False) == FailureClass.QUOTA_EXHAUSTED
    assert classify_output(1, "insufficient_quota on current plan", timed_out=False, cancelled=False) == FailureClass.QUOTA_EXHAUSTED
    assert classify_output(1, "tokens per day limit reached", timed_out=False, cancelled=False) == FailureClass.QUOTA_EXHAUSTED


def test_auth_patterns():
    assert classify_output(1, "Not logged in. Please run login", timed_out=False, cancelled=False) == FailureClass.AUTH
    assert classify_output(1, "401 Unauthorized", timed_out=False, cancelled=False) == FailureClass.AUTH
    assert classify_output(1, "invalid api key provided", timed_out=False, cancelled=False) == FailureClass.AUTH


def test_human_input_patterns_f22():
    # F-22: relax regex to match question mark and other real prompt shapes
    assert classify_output(0, "Do you want to proceed? [y/N]", timed_out=False, cancelled=False) == FailureClass.HUMAN_INPUT
    assert classify_output(0, "Do you want to continue? [y/n]", timed_out=False, cancelled=False) == FailureClass.HUMAN_INPUT
    assert classify_output(0, "Overwrite existing file? (y/n)", timed_out=False, cancelled=False) == FailureClass.HUMAN_INPUT
    assert classify_output(0, "Press any key to continue...", timed_out=False, cancelled=False) == FailureClass.HUMAN_INPUT
    assert classify_output(0, "waiting for user input", timed_out=False, cancelled=False) == FailureClass.HUMAN_INPUT


def test_timeout_and_cancel():
    assert classify_output(None, "", timed_out=True, cancelled=False) == FailureClass.TIMEOUT
    assert classify_output(None, "", timed_out=False, cancelled=True) == FailureClass.CANCELLED


def test_crash_and_clean():
    assert classify_output(2, "segmentation fault", timed_out=False, cancelled=False) == FailureClass.CRASH
    assert classify_output(0, "all good", timed_out=False, cancelled=False) == FailureClass.NONE


def test_auth_beats_rate_limit_when_both_present():
    out = "401 unauthorized; also 429 mentioned"
    assert classify_output(1, out, timed_out=False, cancelled=False) == FailureClass.AUTH


def test_f07_exit_zero_false_positives_suppressed():
    """F-07: exit 0 runs writing HTTP error handling, docs, or explanations
    must NOT be misclassified as rate limits or auth errors."""
    adversarial_exit_zero_outputs = [
        "def handle_rate_limit(resp):\n    if resp.status_code == 429:\n        time.sleep(2)",
        "Added error handling for 401 Unauthorized responses from backend",
        "README: if you see quota exceeded error in production, check your license key",
        "Result: implemented client with retry for too many requests and 503 service unavailable",
        "Commit: fix 429 retry backoff in payment gateway",
        "Documentation: authentication required header format",
    ]
    for output in adversarial_exit_zero_outputs:
        result = classify_output(0, output, timed_out=False, cancelled=False)
        assert result == FailureClass.NONE, f"Misclassified exit 0 output as {result}: {output}"


def test_f07_provider_success_markers():
    from orchestrator.providers.agy import AgyAdapter
    from orchestrator.providers.claude import ClaudeAdapter
    from orchestrator.providers.codex import CodexAdapter
    from orchestrator.providers.opencode import OpencodeAdapter

    claude = ClaudeAdapter()
    codex = CodexAdapter()
    agy = AgyAdapter()
    opencode = OpencodeAdapter()

    # Claude success marker
    claude_tail = '{"type":"result","subtype":"success","result":"Handled 429 error"}'
    assert claude.is_success_marker(claude_tail)
    assert classify_output(0, claude_tail, timed_out=False, cancelled=False, adapter=claude) == FailureClass.NONE

    # Codex success marker
    codex_tail = '{"type":"turn.completed","item":{"text":"Added 429 and 401 handlers"}}'
    assert codex.is_success_marker(codex_tail)
    assert classify_output(0, codex_tail, timed_out=False, cancelled=False, adapter=codex) == FailureClass.NONE

    # Agy success marker
    agy_tail = '{"type":"result","result":"fixed 429 rate limit issue"}'
    assert agy.is_success_marker(agy_tail)
    assert classify_output(0, agy_tail, timed_out=False, cancelled=False, adapter=agy) == FailureClass.NONE

    # Opencode success marker
    opencode_tail = '{"type":"finish","part":{"type":"text","text":"added handling for 429 too many requests"}}'
    assert opencode.is_success_marker(opencode_tail)
    assert classify_output(0, opencode_tail, timed_out=False, cancelled=False, adapter=opencode) == FailureClass.NONE


def test_claude_telemetry_success_not_misclassified():
    tail = (
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning","resetsAt":1788890400}}\n'
        '{"type":"result","subtype":"success","is_error":false,"result":"ORCHESTRATOR_OK"}'
    )
    assert classify_output(0, tail, timed_out=False, cancelled=False) == FailureClass.NONE


def test_codex_turn_failed_is_rate_limit():
    tail = '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Upgrade to Pro"}}'
    assert classify_output(1, tail, timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT


def test_agy_success_status():
    tail = '{"event":"result","result":{"status":"SUCCESS","response":"ORCHESTRATOR_OK"}}'
    assert classify_output(0, tail, timed_out=False, cancelled=False) == FailureClass.NONE
