"""Provider-stated reset times raise the rate-limit cooldown floor (dogfood 2026-09-12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import make_config
from orchestrator.db import Database
from orchestrator.models import FailureClass
from orchestrator.providers.classify import MAX_PROVIDER_RESET_S, parse_reset_after
from orchestrator.providers.fake import FakeAdapter
from orchestrator.providers.registry import ProviderRegistry

CODEX_LIMIT = (
    '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Upgrade to Pro '
    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase "
    'more credits or try again at 6:37 AM."}}'
)


def _local(hour: int, minute: int) -> datetime:
    return datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)


def test_parses_real_codex_wall_clock_message() -> None:
    now = _local(6, 18)  # the dogfood run failed 19 minutes before the stated reset
    assert parse_reset_after(CODEX_LIMIT, now) == 19 * 60


def test_wall_clock_in_the_past_means_tomorrow() -> None:
    now = _local(7, 0)
    assert parse_reset_after("usage limit reached, try again at 6:37 AM", now) == (24 * 60 - 23) * 60


def test_pm_24h_and_ambiguous_forms() -> None:
    now = _local(12, 0)
    assert parse_reset_after("rate limit reached; try again at 1:00 PM", now) == 3600
    assert parse_reset_after("rate limit reached; try again at 13:30", now) == 90 * 60
    assert parse_reset_after("rate limit reached; try again at 12:30 a.m.", _local(0, 0)) == 30 * 60
    # No AM/PM: the sooner of 6:37 and 18:37 from 10:00 is 18:37.
    assert parse_reset_after("usage limit reached, try again at 6:37", _local(10, 0)) == (8 * 60 + 37) * 60


def test_relative_and_compound_forms() -> None:
    assert parse_reset_after("Rate limit exceeded. Try again in 20 minutes.") == 1200
    assert parse_reset_after("429 Too Many Requests, retry-after: 45") == 45
    three_days = parse_reset_after("You've hit your usage limit. Try again in 3 days 4 hours 9 minutes.", cap_s=10**7)
    assert three_days == 3 * 86400 + 4 * 3600 + 9 * 60


def test_absent_or_nonsense_hint_is_none() -> None:
    assert parse_reset_after("") is None
    assert parse_reset_after("429 Too Many Requests") is None
    assert parse_reset_after("rate limit reached; try again at 99:99") is None


def test_hint_is_capped() -> None:
    assert parse_reset_after("rate limit exceeded, try again in 400 hours") == MAX_PROVIDER_RESET_S


def test_model_prose_cannot_set_the_cooldown() -> None:
    """Security/architecture round 4: only limit lines, last one wins."""
    tail = "\n".join(
        [
            "The README says: retry after 86400 seconds if things fail.",
            "usage limit reached; try again at 11:59",
            "Error: rate limit reached, try again in 2 minutes",
        ]
    )
    assert parse_reset_after(tail) == 120
    # A line with only a reset phrase and a limit-signal-free context is ignored.
    assert parse_reset_after("The build log says: see docs, retry after 999 seconds") is None


def test_parsing_is_fast_on_hostile_lines() -> None:
    import time

    hostile = "rate limit reached, try again in " + "1 s " * 2000 + "!"
    started = time.perf_counter()
    parse_reset_after(hostile)
    assert time.perf_counter() - started < 0.5


def _registry(tmp_path: Path) -> tuple[ProviderRegistry, Database]:
    db = Database(tmp_path / "reg.db")
    registry = ProviderRegistry(db, {"codex": FakeAdapter("codex", ["ok"])}, make_config(providers=["codex"]))
    if not db.get("providers", "codex", key="name"):
        db.insert("providers", {"name": "codex", "state": "AVAILABLE"})
    return registry, db


def _cooldown_s(db: Database) -> float:
    row = db.get("providers", "codex", key="name")
    assert row and row["cooldown_until"]
    return (datetime.fromisoformat(row["cooldown_until"]) - datetime.now(UTC)).total_seconds()


def test_stated_reset_raises_rate_limit_cooldown(tmp_path: Path) -> None:
    registry, db = _registry(tmp_path)
    registry.record_failure("codex", FailureClass.RATE_LIMIT, 5.0, "limit", stated_reset_s=1140)
    assert 1100 < _cooldown_s(db) <= 1140
    # The operator can see why the cooldown is longer than the backoff.
    assert db.get("providers", "codex", key="name")["last_error"].startswith("provider-stated reset in 1140s")


def test_stated_reset_is_capped_by_config(tmp_path: Path) -> None:
    db = Database(tmp_path / "cap.db")
    config = make_config(providers=["codex"])
    config._data.setdefault("orchestration", {})["stated_reset_max_seconds"] = 600
    registry = ProviderRegistry(db, {"codex": FakeAdapter("codex", ["ok"])}, config)
    if not db.get("providers", "codex", key="name"):
        db.insert("providers", {"name": "codex", "state": "AVAILABLE"})
    registry.record_failure("codex", FailureClass.RATE_LIMIT, 5.0, "limit", stated_reset_s=86400)
    assert _cooldown_s(db) <= 600


def test_stated_reset_never_shortens_backoff(tmp_path: Path) -> None:
    registry, db = _registry(tmp_path)
    registry.record_failure("codex", FailureClass.QUOTA_EXHAUSTED, 5.0, "quota", stated_reset_s=1)
    # The configured quota floor (default 4 h) still applies; a short hint cannot lower it.
    assert _cooldown_s(db) > timedelta(hours=3).total_seconds()


def test_stated_reset_ignored_for_non_limit_failures(tmp_path: Path) -> None:
    registry, db = _registry(tmp_path)
    registry.record_failure("codex", FailureClass.CRASH, 1.0, "boom", stated_reset_s=3600)
    assert _cooldown_s(db) < 3600


def test_model_stream_events_are_never_read_even_when_last() -> None:
    """Security round 5: an assistant event after the real limit line."""
    tail = "\n".join(
        [
            '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}',
            '{"type":"assistant","message":{"content":"rate limit reached, try again at 11:59"}}',
            '{"event":"result","result":{"response":"usage limit reached, try again in 20 hours"}}',
        ]
    )
    assert parse_reset_after(tail) is None


def test_structured_error_payload_is_read() -> None:
    now = _local(6, 18)
    assert parse_reset_after(CODEX_LIMIT, now) == 19 * 60
    assert parse_reset_after('{"type":"error","message":"429 rate limit, retry after 30"}') == 30


def test_default_cap_is_the_quota_floor(tmp_path: Path) -> None:
    registry, db = _registry(tmp_path)
    registry.record_failure("codex", FailureClass.RATE_LIMIT, 5.0, "limit", stated_reset_s=86400)
    assert _cooldown_s(db) <= 14400


def test_cut_fragment_at_start_of_full_tail_is_ignored() -> None:
    """Security round 6: a model event cut by the tail window reads as plain text."""
    from orchestrator.providers.classify import RAW_TAIL_CHARS

    fragment = "x" * 200 + 'rate limit reached, try again at 11:59"}}'
    result = '{"event":"result","result":{"status":"FAILED"}}'
    tail = (fragment + "\n" + result).rjust(RAW_TAIL_CHARS, "y")[-RAW_TAIL_CHARS:]
    tail = tail[len(tail) - RAW_TAIL_CHARS :]
    assert len(tail) == RAW_TAIL_CHARS
    assert parse_reset_after(tail) is None


def test_unparseable_json_like_line_is_skipped() -> None:
    assert parse_reset_after('{"type":"assistant","text":"rate limit reached, try again in 9 hours"') is None
