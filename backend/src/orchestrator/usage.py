"""Normalized provider usage parsers (Increment 1).

Every value carries provenance. ``None`` means unknown — never zero-fill.
Cumulative session counters are never summed; only the final authoritative
snapshot (or an interval delta for resumed sessions) is used.

Sources: PROVIDER_REPORTED | CLI_REPORTED | LOCALLY_ESTIMATED | UNKNOWN
Completeness: COMPLETE | PARTIAL | UNKNOWN
Bases: FINAL_INVOCATION | PER_STEP | CUMULATIVE_SESSION_SNAPSHOT |
       LOCAL_PROMPT_ESTIMATE | UNKNOWN
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PARSER_VERSION = "usage-v1"


@dataclass
class UsageSummary:
    input_tokens_total: int | None = None
    output_tokens_total: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    native_total_tokens: int | None = None
    source: str = "UNKNOWN"
    completeness: str = "UNKNOWN"
    input_basis: str = "UNKNOWN"
    output_basis: str = "UNKNOWN"
    parser_version: str = PARSER_VERSION
    evidence_kind: str = ""
    observations_count: int = 0
    observed_model: str | None = None
    requested_model: str | None = None
    native_counts: dict[str, Any] = field(default_factory=dict)


def _nonneg(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def unknown_usage(evidence_kind: str = "") -> UsageSummary:
    return UsageSummary(source="UNKNOWN", completeness="UNKNOWN", evidence_kind=evidence_kind)


def _json_lines(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


# -- Claude -----------------------------------------------------------------
# Final ``result.usage`` is authoritative. Per-message assistant usage is
# provisional and must not be summed into a total without terminal evidence.


def parse_claude_usage(stdout_lines: list[str]) -> UsageSummary:
    events = _json_lines(stdout_lines)
    result_events = [e for e in events if e.get("type") == "result"]
    if not result_events:
        # Provisional assistant usage exists but no terminal record.
        return UsageSummary(
            source="CLI_REPORTED",
            completeness="UNKNOWN",
            input_basis="UNKNOWN",
            output_basis="UNKNOWN",
            evidence_kind="claude:no-final-result",
            observations_count=len([e for e in events if e.get("type") == "assistant"]),
        )
    final = result_events[-1]
    raw_usage = final.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    raw_model_usage = final.get("modelUsage")
    model_usage: dict[str, Any] | None = raw_model_usage if isinstance(raw_model_usage, dict) else None
    observed_model = final.get("model")
    if not isinstance(observed_model, str):
        observed_model = None
    inp = _nonneg(usage.get("input_tokens"))
    outp = _nonneg(usage.get("output_tokens"))
    cache_create = _nonneg(usage.get("cache_creation_input_tokens"))
    cache_read = _nonneg(usage.get("cache_read_input_tokens"))
    # Thinking is a subset of output; never add twice.
    thinking: int | None = None
    raw_server = final.get("server_tool_use")
    server_meta: dict[str, Any] | None = raw_server if isinstance(raw_server, dict) else None
    _ = server_meta
    # Some CLIs nest thinking under usage; accept both spellings defensively.
    for key in ("thinking_tokens", "reasoning_tokens"):
        candidate = _nonneg(usage.get(key))
        if candidate is not None:
            thinking = candidate
            break
    normalized_input: int | None = None
    if inp is not None or cache_create is not None or cache_read is not None:
        normalized_input = (inp or 0) + (cache_create or 0) + (cache_read or 0)
    native: dict[str, Any] = {"usage": {k: v for k, v in usage.items() if isinstance(v, (int, float, str))}}
    if isinstance(model_usage, dict):
        # Corroborating breakdown only; never added to the run total.
        safe_breakdown: dict[str, Any] = {}
        for model_name, entry in list(model_usage.items())[:16]:
            if isinstance(entry, dict):
                safe_breakdown[str(model_name)] = {k: v for k, v in entry.items() if isinstance(v, (int, float, str))}
        native["modelUsage"] = safe_breakdown
    completeness = "COMPLETE" if (normalized_input is not None and outp is not None) else "PARTIAL"
    return UsageSummary(
        input_tokens_total=normalized_input,
        output_tokens_total=outp,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_create,
        reasoning_output_tokens=thinking,
        native_total_tokens=None,
        source="PROVIDER_REPORTED",
        completeness=completeness,
        input_basis="FINAL_INVOCATION",
        output_basis="FINAL_INVOCATION",
        evidence_kind="claude:result.usage",
        observations_count=len(result_events),
        observed_model=observed_model,
        native_counts=native,
    )


# -- OpenCode ---------------------------------------------------------------
# ``step_finish``/``step_finish``-shaped token records are per-step deltas.
# Deduplicate by step/session identity; sum completed-step deltas only.
# An unfinished run is PARTIAL, never complete.


def parse_opencode_usage(stdout_lines: list[str]) -> UsageSummary:
    events = _json_lines(stdout_lines)
    seen: set[str] = set()
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_write = 0
    total_reasoning = 0
    total_native = 0
    steps = 0
    unfinished = 0
    observed_model: str | None = None
    for event in events:
        etype = str(event.get("type") or "")
        raw_part = event.get("part")
        part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}
        session_id = str(event.get("sessionID") or event.get("session_id") or "")
        step_id = str(part.get("id") or event.get("id") or "")
        raw_tokens = part.get("tokens")
        tokens: dict[str, Any] | None = raw_tokens if isinstance(raw_tokens, dict) else None
        if tokens is None:
            raw_evt_tokens = event.get("tokens")
            if isinstance(raw_evt_tokens, dict):
                tokens = raw_evt_tokens
        if not isinstance(tokens, dict):
            # step_start without finish contributes to incompleteness only.
            if etype in ("step_start", "step_started") or part.get("type") == "step-start":
                unfinished += 1
            continue
        # Only step-finish-shaped records carry whole-step deltas.
        part_type = part.get("type")
        is_finish = (
            etype in ("step_finish", "step-finish", "finish", "done")
            or part_type in ("step-finish", "step_finish")
            or "step_finish" in etype
        )
        if not is_finish:
            continue
        identity = f"{session_id}\x00{step_id}\x00{json.dumps(tokens, sort_keys=True)}"
        if identity in seen:
            continue
        seen.add(identity)
        # OpenCode fixture semantics: input/cache/output/reasoning are
        # separately additive for normalization in observed fixtures.
        inp = _nonneg(tokens.get("input")) or 0
        outp = _nonneg(tokens.get("output")) or 0
        reasoning = _nonneg(tokens.get("reasoning")) or 0
        raw_cache = tokens.get("cache")
        cache: dict[str, Any] = raw_cache if isinstance(raw_cache, dict) else {}
        cache_read = _nonneg(cache.get("read")) or 0
        cache_write = _nonneg(cache.get("write")) or 0
        native_total = _nonneg(tokens.get("total"))
        total_in += inp
        total_out += outp
        total_reasoning += reasoning
        total_cache_read += cache_read
        total_cache_write += cache_write
        if native_total is not None:
            total_native += native_total
        steps += 1
        model = part.get("model") or event.get("model")
        if isinstance(model, str) and observed_model is None:
            observed_model = model
    if steps == 0:
        return UsageSummary(
            source="CLI_REPORTED" if unfinished else "UNKNOWN",
            completeness="UNKNOWN",
            input_basis="UNKNOWN",
            output_basis="UNKNOWN",
            evidence_kind="opencode:no-step-finish",
            observations_count=unfinished,
        )
    completeness = "PARTIAL" if unfinished else "COMPLETE"
    return UsageSummary(
        input_tokens_total=total_in or None,
        output_tokens_total=(total_out + total_reasoning) or None,
        cache_read_input_tokens=total_cache_read or None,
        cache_write_input_tokens=total_cache_write or None,
        reasoning_output_tokens=total_reasoning or None,
        native_total_tokens=total_native or None,
        source="CLI_REPORTED",
        completeness=completeness,
        input_basis="PER_STEP",
        output_basis="PER_STEP",
        evidence_kind="opencode:step-finish",
        observations_count=steps,
        observed_model=observed_model,
        native_counts={"steps": steps, "unfinished": unfinished},
    )


# -- Codex -------------------------------------------------------------------
# Terminal stdout rarely carries exact usage. Local session artifacts may
# expose CUMULATIVE counters that must never be summed. Only the final
# authoritative snapshot is used. Unattributable sessions yield UNKNOWN.


def parse_codex_stdout_usage(stdout_lines: list[str]) -> UsageSummary:
    events = _json_lines(stdout_lines)
    # Accept a verified terminal usage shape if present; ignore intermediate
    # progress items (item.completed/agent_message are not usage).
    terminal: dict[str, Any] | None = None
    for event in reversed(events):
        etype = str(event.get("type") or event.get("msg", {}).get("type") if isinstance(event.get("msg"), dict) else "")
        if etype in ("turn.completed", "turn_completed", "final", "result"):
            usage = event.get("usage") or event.get("tokens") or event.get("token_usage")
            if isinstance(usage, dict):
                terminal = usage
                break
    if terminal is None:
        return unknown_usage("codex:no-terminal-usage")
    inp = _nonneg(terminal.get("input_tokens") or terminal.get("input"))
    outp = _nonneg(terminal.get("output_tokens") or terminal.get("output"))
    cached = _nonneg(terminal.get("cached_input_tokens") or terminal.get("cached"))
    reasoning = _nonneg(terminal.get("reasoning_tokens") or terminal.get("reasoning"))
    total = _nonneg(terminal.get("total_tokens") or terminal.get("total"))
    if inp is None and outp is None and total is None:
        return unknown_usage("codex:unrecognized-terminal-usage")
    return UsageSummary(
        input_tokens_total=inp,
        output_tokens_total=outp,
        cache_read_input_tokens=cached,
        reasoning_output_tokens=reasoning,
        native_total_tokens=total,
        source="CLI_REPORTED",
        completeness="COMPLETE" if (inp is not None and outp is not None) else "PARTIAL",
        input_basis="FINAL_INVOCATION",
        output_basis="FINAL_INVOCATION",
        evidence_kind="codex:terminal-usage",
        observations_count=1,
        native_counts={k: v for k, v in terminal.items() if isinstance(v, (int, float, str))},
    )


def parse_codex_cumulative_snapshot(
    snapshots: list[dict[str, Any]],
    *,
    attributable: bool,
) -> UsageSummary:
    """Reduce cumulative Codex session token counters.

    Each snapshot is cumulative for its session. The final snapshot wins;
    snapshots are never summed. Unattributable sessions yield UNKNOWN.
    """
    if not attributable or not snapshots:
        return unknown_usage("codex:unattributable-session")
    final = snapshots[-1]
    counts = final.get("token_count") if isinstance(final.get("token_count"), dict) else final
    if not isinstance(counts, dict):
        return unknown_usage("codex:malformed-session")
    inp = _nonneg(counts.get("input_tokens") or counts.get("input"))
    cached = _nonneg(counts.get("cached_input_tokens") or counts.get("cached"))
    outp = _nonneg(counts.get("output_tokens") or counts.get("output"))
    reasoning = _nonneg(counts.get("reasoning_output_tokens") or counts.get("reasoning"))
    total = _nonneg(counts.get("total_tokens") or counts.get("total"))
    model = final.get("model")
    observed = str(model) if isinstance(model, str) else None
    if inp is None and outp is None and total is None:
        return unknown_usage("codex:empty-session")
    return UsageSummary(
        input_tokens_total=inp,
        output_tokens_total=outp,
        cache_read_input_tokens=cached,
        reasoning_output_tokens=reasoning,
        native_total_tokens=total,
        source="CLI_REPORTED",
        completeness="COMPLETE" if (inp is not None and outp is not None) else "PARTIAL",
        input_basis="CUMULATIVE_SESSION_SNAPSHOT",
        output_basis="CUMULATIVE_SESSION_SNAPSHOT",
        evidence_kind="codex:session-snapshot",
        observations_count=len(snapshots),
        observed_model=observed,
        native_counts={k: v for k, v in counts.items() if isinstance(v, (int, float, str))},
    )


# -- AGY ----------------------------------------------------------------------
# No verified usage-bearing fixture exists. Always UNKNOWN.


def parse_agy_usage(stdout_lines: list[str]) -> UsageSummary:
    """AGY's terminal ``{"event": "result", "result": {"usage": {...}}}`` line.

    Observed (dogfood 2026-09-12): ``total_tokens == input_tokens +
    output_tokens`` and ``cache_read_tokens`` exceeds ``input_tokens``, so cache
    reads are separate from input and are added to the normalized input like
    Claude's. Whether ``thinking_tokens`` is inside ``output_tokens`` is not
    stated, so the summary is PARTIAL rather than claiming a complete output.
    """
    events = _json_lines(stdout_lines)
    finals = [e for e in events if e.get("event") == "result" and isinstance(e.get("result"), dict)]
    if not finals:
        return unknown_usage("agy:telemetry-not-captured")
    raw_usage = finals[-1]["result"].get("usage")
    if not isinstance(raw_usage, dict):
        return unknown_usage("agy:result-without-usage")
    inp = _nonneg(raw_usage.get("input_tokens"))
    outp = _nonneg(raw_usage.get("output_tokens"))
    cache_read = _nonneg(raw_usage.get("cache_read_tokens"))
    thinking = _nonneg(raw_usage.get("thinking_tokens"))
    total = _nonneg(raw_usage.get("total_tokens"))
    # Unknown input stays unknown: cache reads alone are not an input total.
    normalized_input = inp + (cache_read or 0) if inp is not None else None
    return UsageSummary(
        input_tokens_total=normalized_input,
        output_tokens_total=outp,
        cache_read_input_tokens=cache_read,
        reasoning_output_tokens=thinking,
        native_total_tokens=total,
        source="PROVIDER_REPORTED",
        completeness="PARTIAL",
        input_basis="FINAL_INVOCATION",
        output_basis="FINAL_INVOCATION",
        evidence_kind="agy:result.usage",
        observations_count=len(finals),
        native_counts={
            "usage": {
                str(k)[:64]: v for k, v in raw_usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        },
    )


def parse_usage_for_provider(provider: str, stdout_lines: list[str]) -> UsageSummary:
    if provider == "claude":
        return parse_claude_usage(stdout_lines)
    if provider == "opencode":
        return parse_opencode_usage(stdout_lines)
    if provider == "codex":
        return parse_codex_stdout_usage(stdout_lines)
    if provider == "agy":
        return parse_agy_usage(stdout_lines)
    return unknown_usage(f"{provider}:unsupported")
