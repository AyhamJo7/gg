# ADR 0004: Provider Failure Classification

## Status
Accepted (implemented, corrected by live evidence)

## Context
Failover quality depends on telling "hit a usage limit" apart from "crashed",
"needs login", and "done". CLIs emit telemetry that *looks* like failures
(e.g. claude's `rate_limit_event` with status `allowed_warning`).

## Decision
`providers/classify.py` maps ordered regex patterns over the output tail +
exit code to `FailureClass`. Ordering: AUTH → RATE_LIMIT → OVERLOADED →
HUMAN_INPUT → CRASH. Two hard-won rules, both covered by tests with captured
real output:
1. Exit 0 **with an explicit success marker** (`"subtype":"success"` or
   `"status":"SUCCESS"`) overrides pattern matches — telemetry is not failure.
2. Bare "rate limit" prose is not classified; only blocking phrasings
   ("hit your usage limit", 429, "limit reached", blocked statuses) are.

Cooldowns are exponential and inferred from real behavior (base 60s × 2^n,
cap 1h, configurable) — we never assume provider reset schedules.

## Consequences
- Codex's real usage-limit event during the dogfood mission classified correctly
  and triggered failover; claude's telemetry false-positive was found by smoke
  test and fixed before it could poison cooldown state.
- New CLI versions may need pattern updates; smoke tests exist for that.
