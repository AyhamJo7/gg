"""Atomic provider reservation system.

Prevents two scheduler decisions from accidentally assigning the same limited
provider concurrently beyond its configured capacity.  Reservations are durable
in SQLite so restart recovery can reconstruct them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .models import EventType, ProviderState, TaskStatus, utcnow

if TYPE_CHECKING:
    from .db import Database
    from .events import EventBus

logger = logging.getLogger(__name__)


def _provider_concurrency_limits(config: dict[str, Any]) -> dict[str, int]:
    """Extract per-provider max concurrency from config."""
    defaults: dict[str, int] = {
        "claude": 1,
        "codex": 1,
        "agy": 1,
        "opencode": 1,
    }
    overrides = config.get("scheduler", {}).get("max_parallel_per_provider", {})
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if isinstance(v, int):
                defaults[k] = v
    return defaults


def _global_max_parallel(config: dict[str, Any]) -> int:
    return int(config.get("scheduler", {}).get("max_parallel_tasks", 3))


def provider_score(
    provider: str,
    task_role: str,
    config: dict[str, Any],
    db: Database,
    independence_bonus: float = 0.0,
) -> tuple[float, list[str]]:
    """Calculate an arbitration score for assigning a provider to a task.

    Higher is better.  The score is observable, deterministic, and explainable.
    """
    score = 0.0
    reasons: list[str] = []

    # 1. Configured role priority
    role_priorities: dict[str, list[str]] = config.get("priority", {})
    priorities = role_priorities.get(task_role, [])
    if provider in priorities:
        idx = priorities.index(provider)
        role_score = max(0.0, 10.0 - idx * 2.0)
        score += role_score
        reasons.append(f"role priority #{idx + 1} (+{role_score:.1f})")

    # 2. Historical success rate
    row = db.get("providers", provider, key="name")
    if row:
        total = int(row.get("total_runs", 0))
        successful = int(row.get("successful_runs", 0))
        if total > 0:
            success_rate = successful / total
            hist_score = success_rate * 5.0
            score += hist_score
            reasons.append(f"historical success {success_rate:.0%} (+{hist_score:.1f})")

        # 3. Cooldown / recent failure penalty
        cooldown = row.get("cooldown_until")
        if cooldown:
            try:
                until = datetime.fromisoformat(cooldown)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                if datetime.now(UTC) < until:
                    penalty = 20.0
                    score -= penalty
                    reasons.append(f"cooldown active (-{penalty:.1f})")
            except ValueError:
                pass

        consec = int(row.get("consecutive_failures", 0))
        if consec > 0:
            fail_penalty = min(consec * 3.0, 15.0)
            score -= fail_penalty
            reasons.append(f"recent failures {consec} (-{fail_penalty:.1f})")

    # 4. Independence bonus
    if independence_bonus:
        score += independence_bonus
        reasons.append(f"independence bonus (+{independence_bonus:.1f})")

    return score, reasons


def try_reserve_provider(
    db: Database,
    events: EventBus,
    task_id: str,
    provider: str,
    config: dict[str, Any],
) -> bool:
    """Atomically reserve a provider for a task.

    Returns True if the reservation succeeded, False if the provider is at
    capacity or ineligible.
    """
    limits = _provider_concurrency_limits(config)
    max_for_provider = limits.get(provider, 1)
    global_max = _global_max_parallel(config)

    # Atomic check-and-reserve inside a transaction
    with db.cursor() as cur:
        # Count active reservations for this provider
        active = cur.execute(
            "SELECT COUNT(*) as cnt FROM provider_reservations WHERE provider=? AND released_at IS NULL",
            (provider,),
        ).fetchone()["cnt"]

        if active >= max_for_provider:
            logger.debug("provider %s at capacity (%d/%d)", provider, active, max_for_provider)
            return False

        # Count global active reservations
        global_active = cur.execute(
            "SELECT COUNT(*) as cnt FROM provider_reservations WHERE released_at IS NULL"
        ).fetchone()["cnt"]

        if global_active >= global_max:
            logger.debug("global parallel capacity reached (%d/%d)", global_active, global_max)
            return False

        # Verify provider is still eligible
        prov = cur.execute("SELECT state, installed FROM providers WHERE name=?", (provider,)).fetchone()
        if (
            not prov
            or not prov["installed"]
            or prov["state"]
            not in (
                ProviderState.AVAILABLE.value,
                ProviderState.COMPLETED.value,
            )
        ):
            logger.debug("provider %s no longer eligible", provider)
            return False

        # Acquire reservation
        reservation_id = f"res-{utcnow().timestamp()}".replace(".", "")
        cur.execute(
            "INSERT INTO provider_reservations(id, task_id, provider, reserved_at) VALUES (?,?,?,?)",
            (reservation_id, task_id, provider, utcnow().isoformat()),
        )

    # Update task status
    db.update("tasks", task_id, {"status": TaskStatus.CLAIMED.value, "assigned_provider": provider})
    events.publish(EventType.PROVIDER_RESERVED, task_id=task_id, provider=provider)
    logger.info("reserved provider %s for task %s", provider, task_id)
    return True


def release_provider_reservation(
    db: Database,
    events: EventBus,
    task_id: str,
    run_id: str | None = None,
) -> None:
    """Release a provider reservation for a task."""
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT id, provider FROM provider_reservations WHERE task_id=? AND released_at IS NULL",
            (task_id,),
        ).fetchone()
        if row:
            cur.execute(
                "UPDATE provider_reservations SET released_at=?, run_id=? WHERE id=?",
                (utcnow().isoformat(), run_id, row["id"]),
            )
            events.publish(EventType.PROVIDER_RELEASED, task_id=task_id, provider=row["provider"])
            logger.info("released provider %s from task %s", row["provider"], task_id)


def active_reservations_for_provider(db: Database, provider: str) -> int:
    rows = db.query(
        "SELECT COUNT(*) as cnt FROM provider_reservations WHERE provider=? AND released_at IS NULL",
        (provider,),
    )
    return rows[0]["cnt"] if rows else 0


def active_reservations(db: Database) -> list[dict[str, Any]]:
    return db.query(
        """SELECT pr.*, t.mission_id, t.title, t.status as task_status
           FROM provider_reservations pr
           JOIN tasks t ON pr.task_id = t.id
           WHERE pr.released_at IS NULL
           ORDER BY pr.reserved_at ASC"""
    )
