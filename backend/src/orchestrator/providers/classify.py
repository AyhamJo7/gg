"""Failure classification: translate provider-specific signals into normalized states.

Patterns are matched against the tail of combined stdout+stderr plus exit code.
Order matters: first match wins (most specific first).
"""

from __future__ import annotations

import re

from ..models import FailureClass, ProviderState

_AUTH_PATTERNS = re.compile(
    r"(not logged in|please (log ?in|authenticate)|unauthorized|401\b|"
    r"invalid (api[_ ]?key|token|credentials)|authentication (failed|required)|"
    r"oauth.*(expired|invalid)|no credentials)",
    re.IGNORECASE,
)

_QUOTA_PATTERNS = re.compile(
    r"(quota (exceeded|exhausted)|exceeded your current quota|"
    r"insufficient_quota|tokens? per day limit|daily.*limit reached|"
    r"individual quota reached)",
    re.IGNORECASE,
)

_RATE_LIMIT_PATTERNS = re.compile(
    r"(429\b|too many requests|usage limit (reached|exceeded)|"
    r"you'?ve hit your (usage )?limit|rate limit (reached|exceeded)|"
    r"rate_limited|\"status\"\s*:\s*\"(blocked|rejected|exhausted)\"|"
    r"limit reached|try again (at|in|after)|"
    r"tokens? per (minute|second) limit|resource_exhausted)",
    re.IGNORECASE,
)

_OVERLOADED_PATTERNS = re.compile(
    r"(\"overloaded\"|overloaded_error|503\b|service unavailable|at capacity)",
    re.IGNORECASE,
)

_HUMAN_INPUT_PATTERNS = re.compile(
    r"(do you want to (proceed|continue)[?:\s]*\[|"
    r"\[y/n\]|\(y/n\)|\[Y/n\]|press enter to confirm|press any key|"
    r"waiting for (user )?input|interactive mode required)",
    re.IGNORECASE,
)

# Explicit positive blocking signals that can indicate failure even on exit code 0
_BLOCKING_SIGNALS: list[tuple[FailureClass, re.Pattern[str]]] = [
    (
        FailureClass.RATE_LIMIT,
        re.compile(
            r"(\"status\"\s*:\s*\"(blocked|rejected)\"|"
            r"\"type\"\s*:\s*\"turn\.failed\"|"
            r"you'?ve hit your (usage )?limit|"
            r"rate limit (reached|exceeded).*try again|"
            r"rate_limited.*try again)",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.QUOTA_EXHAUSTED,
        re.compile(
            r"(\"status\"\s*:\s*\"exhausted\"|"
            r"exceeded your current quota.*upgrade|"
            r"quota (exceeded|exhausted).*try again)",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.AUTH,
        re.compile(
            r"(please (log ?in|authenticate) to continue|"
            r"invalid (api[_ ]?key|token).*run login|"
            r"authentication (failed|required).*login)",
            re.IGNORECASE,
        ),
    ),
]

FAILURE_TO_STATE: dict[FailureClass, ProviderState] = {
    FailureClass.RATE_LIMIT: ProviderState.RATE_LIMITED,
    FailureClass.QUOTA_EXHAUSTED: ProviderState.RATE_LIMITED,
    FailureClass.AUTH: ProviderState.AUTH_REQUIRED,
    FailureClass.OVERLOADED: ProviderState.UNAVAILABLE,
    FailureClass.TIMEOUT: ProviderState.TIMED_OUT,
    FailureClass.CRASH: ProviderState.CRASHED,
    FailureClass.MALFORMED_OUTPUT: ProviderState.CRASHED,
    FailureClass.HUMAN_INPUT: ProviderState.HUMAN_INPUT_REQUIRED,
    FailureClass.CANCELLED: ProviderState.AVAILABLE,
    FailureClass.NONE: ProviderState.AVAILABLE,
    FailureClass.UNKNOWN: ProviderState.CRASHED,
}


def classify_output(
    exit_code: int | None,
    combined_output: str,
    *,
    timed_out: bool,
    cancelled: bool,
    adapter: object | None = None,
) -> FailureClass:
    if cancelled:
        return FailureClass.CANCELLED
    if timed_out:
        return FailureClass.TIMEOUT

    tail = combined_output[-8000:]

    # 1. Structured success markers (adapter hook or standard schemas)
    if adapter and hasattr(adapter, "is_success_marker") and adapter.is_success_marker(tail):
        return FailureClass.NONE

    normalized = tail.replace(" ", "")
    if exit_code == 0 and (
        '"subtype":"success"' in normalized
        or '"status":"SUCCESS"' in normalized
        or ('"type":"result"' in normalized and '"is_error":true' not in normalized)
        or '"turn.completed"' in normalized
        or '"type":"finish"' in normalized
    ):
        return FailureClass.NONE

    # 2. Exit code 0 is success unless an explicit positive blocking signal is present
    if exit_code == 0:
        for failure_class, pattern in _BLOCKING_SIGNALS:
            if pattern.search(tail):
                return failure_class
        return FailureClass.NONE

    # 3. Human input prompt (only triggered for non-zero exit codes or exit code None)
    if _HUMAN_INPUT_PATTERNS.search(tail):
        return FailureClass.HUMAN_INPUT

    # 4. Non-zero exit code: check specific error patterns
    if _AUTH_PATTERNS.search(tail):
        return FailureClass.AUTH
    if _QUOTA_PATTERNS.search(tail):
        return FailureClass.QUOTA_EXHAUSTED
    if _RATE_LIMIT_PATTERNS.search(tail):
        return FailureClass.RATE_LIMIT
    if _OVERLOADED_PATTERNS.search(tail):
        return FailureClass.OVERLOADED

    if exit_code not in (0, None):
        return FailureClass.CRASH
    return FailureClass.NONE
