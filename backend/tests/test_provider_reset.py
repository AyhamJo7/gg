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
    assert parse_reset_after("try again at 6:37 AM", now) == (24 * 60 - 23) * 60


def test_pm_and_24h_forms() -> None:
    now = _local(12, 0)
    assert parse_reset_after("try again at 1:00 PM", now) == 3600
    assert parse_reset_after("try again at 13:30", now) == 90 * 60
    assert parse_reset_after("try again at 12:30 a.m.", _local(0, 0)) == 30 * 60


def test_relative_forms() -> None:
    assert parse_reset_after("Rate limited. Try again in 20 minutes.") == 1200
    assert parse_reset_after("limit resets in 2 hours") == 7200
    assert parse_reset_after("Retry-After: 45") == 45


def test_absent_or_nonsense_hint_is_none() -> None:
    assert parse_reset_after("") is None
    assert parse_reset_after("429 Too Many Requests") is None
    assert parse_reset_after("try again at 99:99") is None


def test_hint_is_capped() -> None:
    assert parse_reset_after("try again in 400 hours") == MAX_PROVIDER_RESET_S


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


def test_stated_reset_never_shortens_backoff(tmp_path: Path) -> None:
    registry, db = _registry(tmp_path)
    registry.record_failure("codex", FailureClass.QUOTA_EXHAUSTED, 5.0, "quota", stated_reset_s=1)
    # The configured quota floor (default 4 h) still applies; a short hint cannot lower it.
    assert _cooldown_s(db) > timedelta(hours=3).total_seconds()


def test_stated_reset_ignored_for_non_limit_failures(tmp_path: Path) -> None:
    registry, db = _registry(tmp_path)
    registry.record_failure("codex", FailureClass.CRASH, 1.0, "boom", stated_reset_s=3600)
    assert _cooldown_s(db) < 3600
