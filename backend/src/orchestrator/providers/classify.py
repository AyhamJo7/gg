"""Failure classification: translate provider-specific signals into normalized states.

Patterns are matched against the tail of combined stdout+stderr plus exit code.
Order matters: first match wins (most specific first).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

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
    gate_refused: bool = False,
) -> FailureClass:
    if cancelled:
        return FailureClass.CANCELLED
    if timed_out:
        return FailureClass.TIMEOUT
    if gate_refused:
        # The spawn handshake never released — the child exited before ever
        # exec'ing the provider (e.g. backend died between fork and on_spawn
        # persisting identity). Its exit code (GATE_REFUSED_EXIT) and empty
        # output are not a real provider signal and must never be pattern-
        # matched as one — this is an internal crash, not the provider's own
        # exit, so it must never resolve to FailureClass.NONE.
        return FailureClass.CRASH

    tail = combined_output[-8000:]

    # Terminal precedence: an intermediate success-like event must never
    # override a later failure. Explicit terminal failure evidence and a
    # non-zero process exit therefore take precedence over any success
    # marker appearing anywhere in the tail.
    for failure_class, pattern in _BLOCKING_SIGNALS:
        if pattern.search(tail):
            return failure_class

    # Non-zero exit is failure even when an earlier success marker exists.
    if exit_code is not None and exit_code != 0:
        pass  # fall through to specific failure classification below
    else:
        # exit_code == 0: success unless a blocking signal was present
        # (checked above). Structured success markers only confirm success;
        # they are never needed to override a failure.
        if exit_code == 0:
            return FailureClass.NONE
        # exit_code None without cancel/timeout/refusal: no outcome below
        # may claim success; continue to pattern checks then CRASH.

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
    # exit_code None without cancel/timeout/refusal carries no successful
    # process outcome; success markers elsewhere in the tail must not
    # promote it to success.
    if exit_code is None:
        return FailureClass.CRASH
    return FailureClass.NONE


# -- Provider-stated reset times ---------------------------------------------
# Providers often print when a limit lifts ("try again at 6:37 AM", "try
# again in 20 minutes"). The registry uses this only as a floor on its own
# exponential cooldown, never to shorten it. Wall-clock times carry no zone:
# they are read as the host's local time, since the CLI runs on this host.
MAX_PROVIDER_RESET_S = 24 * 60 * 60
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600}
_RESET_AT = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", re.IGNORECASE)
_RESET_IN = re.compile(
    r"(?:try again|retry|resets?)\s+in\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)\b",
    re.IGNORECASE,
)
_RETRY_AFTER = re.compile(r"retry[- ]after[:=\s]+(\d+)\b", re.IGNORECASE)


def parse_reset_after(text: str, now: datetime | None = None) -> float | None:
    """Seconds until the provider says its limit resets, or None if unstated."""
    if not text:
        return None
    local_now = (now or datetime.now(UTC)).astimezone()
    seconds: float | None = None
    if m := _RESET_AT.search(text):
        hour, minute, meridiem = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= local_now:
                target += timedelta(days=1)
            seconds = (target - local_now).total_seconds()
    elif m := _RESET_IN.search(text):
        seconds = float(m.group(1)) * _SECONDS_PER_UNIT[m.group(2)[0].lower()]
    elif m := _RETRY_AFTER.search(text):
        seconds = float(m.group(1))
    if seconds is None or seconds <= 0:
        return None
    return min(seconds, MAX_PROVIDER_RESET_S)
