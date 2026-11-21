# GG

Use [AGENTS.md](AGENTS.md) as the canonical contributor guidance for this checkout.
Start with the current task, `git status`, and relevant source modules.

- Current behavior: [ARCHITECTURE.md](ARCHITECTURE.md).
- Commands: [DEVELOPMENT.md](DEVELOPMENT.md).
- Execution/secret boundaries: [SECURITY.md](SECURITY.md).
- Operator UX history: [docs/OPERATOR_EXPERIENCE_REVIEW.md](docs/OPERATOR_EXPERIENCE_REVIEW.md).
- Proposed improvements, only when needed: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Current source overrides audit proposals and historical reports.

## Phase

Increments 1-4 (invocation integrity, deterministic context compilation, evidence
and provenance, dependency execution correctness, bounded autonomous repair) and
Operator Experience v1 are sealed foundations. GG is now in real-world dogfood and
stabilization, not a new increment. Do not reopen sealed architecture without
reproduced evidence from an actual run, and do not start speculative Increment 5
work. Let observed dogfood evidence decide what changes next.

Preserve what those increments bought: exact-SHA provenance, reviewer independence,
dependency artifact correctness, bounded repair, and operator truthfulness. Backend
and frontend boundaries stay truthful — never fabricate quota, confidence, ETA, or
evidence that was not captured, and never render unknown as zero.

Provider quota is finite and shared with the operator's own work. Keep routine tests
deterministic and provider-free; invoke real providers only when explicitly scoped.

Do not load the full audit or historical reports by default. Keep task context
scoped and preserve user work.
