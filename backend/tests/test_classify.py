from orchestrator.models import FailureClass
from orchestrator.providers.classify import classify_output


def test_rate_limit_patterns():
    assert classify_output(1, "Error: 429 too many requests", timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT
    assert classify_output(0, "You've hit your usage limit, try again at 5pm", timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT
    assert classify_output(1, "resource_exhausted: quota exceeded", timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT
    assert classify_output(1, "insufficient_quota on current plan", timed_out=False, cancelled=False) == FailureClass.RATE_LIMIT


def test_auth_patterns():
    assert classify_output(1, "Not logged in. Please run login", timed_out=False, cancelled=False) == FailureClass.AUTH
    assert classify_output(1, "401 Unauthorized", timed_out=False, cancelled=False) == FailureClass.AUTH
    assert classify_output(1, "invalid api key provided", timed_out=False, cancelled=False) == FailureClass.AUTH


def test_timeout_and_cancel():
    assert classify_output(None, "", timed_out=True, cancelled=False) == FailureClass.TIMEOUT
    assert classify_output(None, "", timed_out=False, cancelled=True) == FailureClass.CANCELLED


def test_crash_and_clean():
    assert classify_output(2, "segmentation fault", timed_out=False, cancelled=False) == FailureClass.CRASH
    assert classify_output(0, "all good", timed_out=False, cancelled=False) == FailureClass.NONE


def test_auth_beats_rate_limit_when_both_present():
    out = "401 unauthorized; also 429 mentioned"
    assert classify_output(1, out, timed_out=False, cancelled=False) == FailureClass.AUTH
