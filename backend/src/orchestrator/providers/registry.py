"""Provider registry: detection, health tracking with cooldown/backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..db import Database
from ..models import FailureClass, ProviderState
from .agy import AgyAdapter
from .base import ProviderAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpencodeAdapter


def build_real_adapters(config: Any) -> dict[str, ProviderAdapter]:
    """Instantiate adapters for providers enabled in config."""
    classes: dict[str, type[ProviderAdapter]] = {
        "claude": ClaudeAdapter,
        "codex": CodexAdapter,
        "agy": AgyAdapter,
        "opencode": OpencodeAdapter,
    }
    adapters: dict[str, ProviderAdapter] = {}
    for name, cls in classes.items():
        executable = config.get(f"providers.{name}.executable", name)
        if config.provider_enabled(name):
            if cls is OpencodeAdapter:
                model = config.get(f"providers.{name}.model")
                adapters[name] = cls(executable=executable, model=model)
            else:
                adapters[name] = cls(executable=executable)
    return adapters


class ProviderRegistry:
    """Runtime provider health + persistence-backed cooldown tracking."""

    def __init__(self, db: Database, adapters: dict[str, ProviderAdapter], config: Any):
        self._db = db
        self.adapters = adapters
        self._config = config

    async def detect_all(self) -> None:
        for name, adapter in self.adapters.items():
            installed, path = adapter.detect()
            version = await adapter.get_version() if installed else None
            existing = self._db.get("providers", name, key="name") or {}
            state = existing.get("state") or ProviderState.UNAVAILABLE.value
            if not installed:
                state = ProviderState.UNAVAILABLE.value
            elif state in (ProviderState.UNAVAILABLE.value, ProviderState.BUSY.value):
                # BUSY at startup is impossible (no runs in flight) — reset.
                state = ProviderState.AVAILABLE.value
            self._db.execute(
                """INSERT INTO providers(name, state, installed, executable_path, version,
                       cooldown_until, consecutive_failures, total_runs, successful_runs,
                       rate_limit_events, total_runtime_seconds, last_run_at, last_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                       state=excluded.state, installed=excluded.installed,
                       executable_path=excluded.executable_path, version=excluded.version""",
                (
                    name,
                    ProviderState.DISABLED.value if not self._config.provider_enabled(name) else state,
                    int(installed),
                    path,
                    version,
                    existing.get("cooldown_until"),
                    existing.get("consecutive_failures", 0),
                    existing.get("total_runs", 0),
                    existing.get("successful_runs", 0),
                    existing.get("rate_limit_events", 0),
                    existing.get("total_runtime_seconds", 0.0),
                    existing.get("last_run_at"),
                    existing.get("last_error"),
                ),
            )

    def health(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM providers ORDER BY name")
        for row in rows:
            if row.get("cooldown_until"):
                until = datetime.fromisoformat(row["cooldown_until"])
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                if datetime.now(UTC) < until:
                    if row["state"] != ProviderState.RATE_LIMITED.value:
                        row["state"] = "COOLDOWN"
                else:
                    # cooldown expired → provider becomes eligible again
                    expired = (
                        ProviderState.RATE_LIMITED.value,
                        ProviderState.TIMED_OUT.value,
                        ProviderState.CRASHED.value,
                    )
                    if row["state"] in expired:
                        self._db.execute(
                            "UPDATE providers SET state=?, cooldown_until=NULL WHERE name=?",
                            (ProviderState.AVAILABLE.value, row["name"]),
                        )
                        row["state"] = ProviderState.AVAILABLE.value
                        row["cooldown_until"] = None
        return rows

    def has_potentially_available(self) -> bool:
        """True if any provider could become eligible later (installed and not
        DISABLED). Cooldown/transient states count as potentially available."""
        rows = self._db.query("SELECT state, installed FROM providers")
        for row in rows:
            if row["installed"] and row["state"] != ProviderState.DISABLED.value:
                return True
        return False

    def is_eligible(self, name: str) -> bool:
        row = self._db.get("providers", name, key="name")
        if not row or not row["installed"] or row["state"] == ProviderState.DISABLED.value:
            return False
        if row.get("cooldown_until"):
            until = datetime.fromisoformat(row["cooldown_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if datetime.now(UTC) < until:
                return False
            # Cooldown expired — lazily reconcile state so eligibility never
            # depends on a scheduler tick having run health() first.
            if row["state"] in (
                ProviderState.RATE_LIMITED.value,
                ProviderState.TIMED_OUT.value,
                ProviderState.CRASHED.value,
                ProviderState.UNAVAILABLE.value,
            ):
                self._db.execute(
                    "UPDATE providers SET state=?, cooldown_until=NULL WHERE name=?",
                    (ProviderState.AVAILABLE.value, name),
                )
                row["state"] = ProviderState.AVAILABLE.value
        # Serial scheduler v1: only AVAILABLE providers are eligible.
        # BUSY/COOLDOWN/RATE_LIMITED/etc. are all excluded.
        return bool(row["state"] == ProviderState.AVAILABLE.value)

    def mark_busy(self, name: str) -> None:
        self._db.execute(
            "UPDATE providers SET state=?, last_run_at=? WHERE name=?",
            (ProviderState.BUSY.value, datetime.now(UTC).isoformat(), name),
        )

    def record_success(self, name: str, runtime_s: float) -> None:
        self._db.execute(
            """UPDATE providers SET state=?, consecutive_failures=0, cooldown_until=NULL,
                   total_runs=total_runs+1, successful_runs=successful_runs+1,
                   total_runtime_seconds=total_runtime_seconds+?, last_error=NULL
               WHERE name=?""",
            (ProviderState.AVAILABLE.value, runtime_s, name),
        )

    def record_failure(self, name: str, failure: FailureClass, runtime_s: float, error: str) -> ProviderState:
        """Apply exponential cooldown. Returns the recorded state."""
        base = float(self._config.get("orchestration.cooldown_base_seconds", 60))
        mult = float(self._config.get("orchestration.cooldown_multiplier", 2.0))
        cap = float(self._config.get("orchestration.cooldown_max_seconds", 3600))
        row = self._db.get("providers", name, key="name") or {}
        failures = int(row.get("consecutive_failures", 0)) + 1
        cooldown_s = min(base * (mult ** (failures - 1)), cap)
        state_by_failure = {
            FailureClass.RATE_LIMIT: ProviderState.RATE_LIMITED,
            FailureClass.QUOTA_EXHAUSTED: ProviderState.RATE_LIMITED,
            FailureClass.AUTH: ProviderState.AUTH_REQUIRED,
            FailureClass.TIMEOUT: ProviderState.TIMED_OUT,
            FailureClass.CRASH: ProviderState.CRASHED,
            FailureClass.MALFORMED_OUTPUT: ProviderState.CRASHED,
            FailureClass.OVERLOADED: ProviderState.UNAVAILABLE,
        }
        state = state_by_failure.get(failure, ProviderState.AVAILABLE)
        cooldown_until: str | None = None
        if state in (ProviderState.RATE_LIMITED, ProviderState.TIMED_OUT, ProviderState.CRASHED,
            ProviderState.UNAVAILABLE):
            cooldown_until = (datetime.now(UTC) + timedelta(seconds=cooldown_s)).isoformat()
        rate_inc = 1 if failure in (FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED) else 0
        self._db.execute(
            """UPDATE providers SET state=?, consecutive_failures=?, cooldown_until=?,
                   total_runs=total_runs+1, rate_limit_events=rate_limit_events+?,
                   total_runtime_seconds=total_runtime_seconds+?, last_error=?, last_run_at=?
               WHERE name=?""",
            (
                state.value,
                failures,
                cooldown_until,
                rate_inc,
                runtime_s,
                error[:500],
                datetime.now(UTC).isoformat(),
                name,
            ),
        )
        return state

    def set_enabled(self, name: str, enabled: bool) -> None:
        state = ProviderState.AVAILABLE if enabled else ProviderState.DISABLED
        self._db.execute("UPDATE providers SET state=? WHERE name=?", (state.value, name))

    def get_adapter(self, name: str) -> ProviderAdapter | None:
        return self.adapters.get(name)
