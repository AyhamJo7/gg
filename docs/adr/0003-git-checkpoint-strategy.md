# ADR 0003: Git Checkpoint Strategy

## Status
Accepted (implemented, dogfood-verified)

## Context
Multi-provider missions need an auditable engineering ledger: who changed what,
when, and the ability to inspect/roll back. User work in existing repositories
must never be destroyed.

## Decision
Git is the ledger. Checkpoints (`git add -A` + commit) happen: before a mission
touches a dirty workspace, after every provider phase, before every provider
switch, and at final verification. Commit messages carry attribution
(`agent(opencode): implementation checkpoint`, `orchestrator: checkpoint before
provider switch`). Sensitive files (`.env*`, keys, credential JSONs) are
force-unstaged before committing and auto-appended to `.gitignore`.
Destructive commands (`reset --hard`, `clean -fd`) are never used.

## Consequences
- Full provider attribution in `git log` (verified in dogfood repo).
- `.env` and friends can never leak into history via the orchestrator.
- Auto-commits on the user's branch are opinionated but explicit and reversible
  with normal git tooling; users who want isolation can work in a branch.
