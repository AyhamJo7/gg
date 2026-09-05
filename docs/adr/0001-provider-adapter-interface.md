# ADR 0001: Provider Adapter Interface

## Status
Accepted (implemented, runtime-verified)

## Context
Four consumer AI subscriptions are only accessible through their local CLIs,
each with different invocation styles, output formats, and failure signals.
The orchestrator needs a uniform execution + classification contract, and the
internet-documented flags must never be trusted over runtime behavior.

## Decision
A single `ProviderAdapter` ABC (`providers/base.py`) with: `detect`,
`get_version`, `get_capabilities`, `build_command` (argv array only),
`normalize_output_line`, `extract_summary`, `execute`, `interrupt`,
`classify_failure`, `health_check`. Concrete adapters per CLI
(`claude.py`, `codex.py`, `agy.py`, `opencode.py`) plus `fake.py` simulation
adapters for deterministic tests. Every adapter was written against
`--help` output and real executions on this machine, not documentation.

## Consequences
- Orchestration logic is provider-agnostic; adding a provider = one file + registration.
- JSON-stream normalization lives in adapters, keeping the engine simple.
- Fake adapters let the entire state machine run E2E in tests without quota.
- Cost: output normalization is best-effort per CLI and must be re-verified on CLI upgrades.
