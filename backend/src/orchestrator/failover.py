"""Failover evidence: a failed attempt's own report, carried to the next provider.

Dogfood 2026-09-12: a rate-limited run's findings vanished and the retry got a
byte-identical prompt. The note is bounded, redacted, and labelled partial and
unverified; compilers emit it as a separate PREFERRED block that never
displaces orchestrator evidence, and reviewers never receive it.
"""

from __future__ import annotations

from typing import Any

from .security import redact

FAILOVER_EVIDENCE_CHARS = 1500
MS_PER_SECOND = 1000
SUCCEEDED_RUN_STATUS = "SUCCEEDED"
NO_FAILURE = "NONE"


def format_failover_note(provider: str, failure: str, duration_s: float | None, report: str) -> str:
    text = redact((report or "").strip())
    if not text:
        return ""
    if len(text) > FAILOVER_EVIDENCE_CHARS:
        text = text[:FAILOVER_EVIDENCE_CHARS] + "…"
    ran = f" after {duration_s:.0f}s" if duration_s is not None else ""
    return (
        f"{provider} stopped with {failure}{ran}. "
        "Its last report is partial and unverified; re-check before relying on it:\n"
        f"{text}"
    )


def failover_note_from_run(run: dict[str, Any] | None) -> str:
    """Note for a task retry, rebuilt from the persisted failed run (restart-safe)."""
    if not run:
        return ""
    failure = str(run.get("failure_class") or "")
    status = str(run.get("run_status") or "")
    if status == SUCCEEDED_RUN_STATUS or (not status and failure in ("", NO_FAILURE)):
        return ""
    duration_ms = run.get("duration_ms")
    duration_s = duration_ms / MS_PER_SECOND if isinstance(duration_ms, int | float) else None
    return format_failover_note(
        str(run.get("provider") or "a provider"),
        failure or status or "an unrecorded failure",
        duration_s,
        str(run.get("summary") or ""),
    )
