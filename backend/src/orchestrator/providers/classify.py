"""Failure classification: translate provider-specific signals into normalized states.

Patterns are matched against the tail of combined stdout+stderr plus exit code.
Order matters: first match wins (most specific first).
"""

from __future__ import annotations

import json
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
# again in 3 days 4 hours"). The registry uses this only as a floor on its own
# exponential cooldown (never to shorten it) and caps it by config. Only lines
# that themselves carry a limit signal are read, and the LAST such line wins:
# model-streamed prose earlier in the tail must not set the provider's
# cooldown. Wall-clock times carry no zone and are read as the host's local
# time (the CLI runs on this host); a 12-hour time without AM/PM takes the
# sooner of its two readings.
MAX_PROVIDER_RESET_S = 24 * 60 * 60
# Bounded combined stdout+stderr tail kept on ExecutionResult.raw_tail.
RAW_TAIL_CHARS = 4000
HOURS_PER_HALF_DAY = 12
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RESET_AT = re.compile(r"try again at\s+(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", re.IGNORECASE)
_DURATION_PART = r"(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b"
_RESET_IN = re.compile(
    rf"(?:try again|retry|resets?)\s+in\s+((?:{_DURATION_PART}[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_DURATION_ITEM = re.compile(_DURATION_PART, re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry[- ]after[:=\s]+(\d+)\b", re.IGNORECASE)


def _is_limit_line(line: str) -> bool:
    return bool(_RATE_LIMIT_PATTERNS.search(line) or _QUOTA_PATTERNS.search(line))


def _wall_clock_seconds(hour: int, minute: int, meridiem: str, now: datetime) -> float | None:
    if minute >= 60:
        return None
    candidates: list[int] = []
    if meridiem:
        if hour > HOURS_PER_HALF_DAY or hour == 0:
            return None
        candidates = [hour % HOURS_PER_HALF_DAY + (HOURS_PER_HALF_DAY if meridiem == "pm" else 0)]
    elif hour < 24:
        candidates = [hour]
        if 0 < hour <= HOURS_PER_HALF_DAY:
            candidates.append((hour + HOURS_PER_HALF_DAY) % 24)
    best: float | None = None
    for h in candidates:
        # Build the wall-clock target naively and localize it, so a reset on
        # the other side of a DST change gets its own UTC offset.
        naive = now.replace(tzinfo=None, hour=h, minute=minute, second=0, microsecond=0)
        target = naive.astimezone()
        if target <= now:
            target = (naive + timedelta(days=1)).astimezone()
        delta = (target - now).total_seconds()
        best = delta if best is None else min(best, delta)
    return best


def _line_reset_seconds(line: str, now: datetime) -> float | None:
    if m := _RESET_AT.search(line):
        meridiem = (m.group(3) or "").lower().replace(".", "")
        return _wall_clock_seconds(int(m.group(1)), int(m.group(2)), meridiem, now)
    if m := _RESET_IN.search(line):
        return sum(
            float(part.group(1)) * _SECONDS_PER_UNIT[part.group(2)[0].lower()]
            for part in _DURATION_ITEM.finditer(m.group(1))
        )
    if m := _RETRY_AFTER.search(line):
        return float(m.group(1))
    return None


_ERROR_EVENT_MARKERS = ("error", "fail")


def _cli_error_text(line: str) -> str | None:
    """Text of a line the CLI (not the model) wrote, or None to skip it.

    Structured stream events are trusted only through their error payload;
    assistant/message/result events carry model-written text and are skipped.
    Plain-text lines cannot be told apart and are read, bounded by the cap.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return line
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        # A broken event is not the CLI's own plain-text error line.
        return None
    if not isinstance(event, dict):
        return None
    if "error" in event:
        return json.dumps(event["error"]) if not isinstance(event["error"], str) else event["error"]
    kind = str(event.get("type") or event.get("event") or "").lower()
    if any(marker in kind for marker in _ERROR_EVENT_MARKERS):
        return stripped
    return None


def parse_reset_after(text: str, now: datetime | None = None, cap_s: float = MAX_PROVIDER_RESET_S) -> float | None:
    """Seconds until the provider's own limit message says it resets, or None."""
    if not text:
        return None
    local_now = (now or datetime.now(UTC)).astimezone()
    lines = text.splitlines()
    if len(text) >= RAW_TAIL_CHARS and lines:
        # A full-size tail starts mid-line: its first line is a fragment of
        # something (often a model event) whose origin cannot be known.
        lines = lines[1:]
    for raw_line in reversed(lines):
        line = _cli_error_text(raw_line)
        if line is None or not _is_limit_line(line):
            continue
        seconds = _line_reset_seconds(line, local_now)
        if seconds is not None and seconds > 0:
            return min(seconds, cap_s)
    return None
