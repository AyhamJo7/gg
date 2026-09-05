"""Failure classification: translate provider-specific signals into normalized states.

Patterns are matched against the tail of combined stdout+stderr plus exit code.
Order matters: first match wins (most specific first).
"""

from __future__ import annotations

import re

from ..models import FailureClass, ProviderState

_PATTERNS: list[tuple[FailureClass, re.Pattern[str]]] = [
    (
        FailureClass.AUTH,
        re.compile(
            r"(not logged in|please (log ?in|authenticate)|unauthorized|401\b|"
            r"invalid (api[_ ]?key|token|credentials)|authentication (failed|required)|"
            r"oauth.*(expired|invalid)|no credentials)",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.RATE_LIMIT,
        re.compile(
            r"(rate[ _-]?limit|429\b|too many requests|usage limit (reached|exceeded)|"
            r"you'?ve hit your (usage )?limit|limit reached|try again (at|in|after)|"
            r"quota exceeded|exceeded your current quota|resource_exhausted|"
            r"insufficient_quota|overloaded_error|529\b|tokens? per (minute|day) limit)",
            re.IGNORECASE,
        ),
    ),
    (
        FailureClass.OVERLOADED,
        re.compile(r"(overloaded|503\b|service unavailable|capacity)", re.IGNORECASE),
    ),
    (
        FailureClass.HUMAN_INPUT,
        re.compile(
            r"(do you want to (proceed|continue)\s*\[|press enter to confirm|"
            r"waiting for (user )?input|interactive mode required)",
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


def classify_output(exit_code: int | None, combined_output: str, *, timed_out: bool, cancelled: bool) -> FailureClass:
    if cancelled:
        return FailureClass.CANCELLED
    if timed_out:
        return FailureClass.TIMEOUT
    tail = combined_output[-8000:]
    for failure_class, pattern in _PATTERNS:
        if pattern.search(tail):
            return failure_class
    if exit_code not in (0, None):
        return FailureClass.CRASH
    return FailureClass.NONE
