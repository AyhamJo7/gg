# NEXT — engineering handoff

Read [AGENTS.md](AGENTS.md) first. GG is a local, single-operator orchestrator of
authenticated provider CLIs, in dogfood/stabilization: sealed Increments 1-4 and
Operator Experience v1 are not reopened without evidence from a real run.

## State

- Branch `feature/operator-experience-v2`, draft PR #6 into `main` (mergeable, no CI
  checks exist: the repo has no `.github/` workflows).
- Progress record: [docs/OPERATOR_EXPERIENCE_V2_PROGRESS.md](docs/OPERATOR_EXPERIENCE_V2_PROGRESS.md);
  every tier and review round 1-7 is closed with no open Critical/High/Medium findings.
- Draft PR #7 (`chore/claude-gates`) adds `.claude/gates.json`; until it merges,
  `gates preflight` fails with "no gates config". Run the Makefile gates directly.

## Verified gates (WSL, Node v26.10.0, Python 3.12)

| Gate | Command | SHA | Result |
|---|---|---|---|
| Backend tests | `make test-backend` (699 tests, ~10.5 min) | 93cbfe1 | RUN+PASSED |
| Backend lint | `uv run ruff check src tests` | 93cbfe1 | RUN+PASSED |
| Backend types | `uv run mypy src` | 93cbfe1 | RUN+PASSED |
| Frontend tests | `npm --prefix frontend run test -- --run` (140 tests) | 4f0c07c | RUN+PASSED |
| Frontend lint | `npm --prefix frontend run lint` | 4f0c07c | RUN+PASSED |
| Frontend types | `npm --prefix frontend run typecheck` | 4f0c07c | RUN+PASSED |
| Browser e2e | `make e2e` | 93cbfe1 | RUN+FAILED (environment, see below) |

The only change between 93cbfe1 and 4f0c07c is `frontend/src/test/setup.ts`; backend
results carry over by diff, not by re-run.

## Done this session

- Found and fixed a baseline failure: on Node 25+ the built-in `localStorage` getter
  returns `undefined` and shadows jsdom's Storage, so 8 `ActivityCommand` tests failed
  on Node 26 independent of the code. `frontend/src/test/setup.ts` now installs an
  in-memory Storage (4f0c07c). Before: 8 failed / 132 passed; after: 140 passed.

## Blocked (human-owned)

1. **Browser e2e not run.** Playwright's Chromium is not installed
   (`~/.cache/ms-playwright/chromium_headless_shell-1243` missing). Run
   `cd frontend && npx playwright install chromium`, then `make e2e` (isolated:
   temp state dir, ports 8799/5174, fake providers, no quota).
2. **PR #6 merge** is a product decision (draft, no CI, no reviewer assigned).
3. **No CI.** There are no GitHub Actions workflows, so nothing enforces the gates
   above on push. Adding one runs on hosted runners; decide before it is built.

## Known gaps (documented, deliberately not built)

- `repair_worker` does not carry failover notes (no reliable retry-to-failed-run link
  without touching sealed Increment 4 semantics). Port only if a dogfood run shows
  duplicated work there.
- Reset hints in JSON notices whose type is neither error nor fail are not read.
- The repo pins no Node version; the suite is verified on v26.10.0 only.

## Next

1. After the human steps above: `make e2e`, then mark PR #6 ready or merge per decision.
2. Further work comes from real dogfood evidence (`docs/dogfood/`), not speculation.

## Resume

```
git status && git log --oneline -5
make lint && make typecheck && make test-frontend
make test-backend          # ~10 min; run in the background
```
