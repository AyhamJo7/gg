# NEXT — engineering handoff

Read [AGENTS.md](AGENTS.md) first. GG is a local, single-operator orchestrator of
authenticated provider CLIs, in dogfood/stabilization: sealed Increments 1-4 and
Operator Experience v1 are not reopened without evidence from a real run.

## State

- Branch `feature/operator-experience-v2`, draft PR #6 into `main` (mergeable). `main`
  has no branch protection and `.github/` holds only the README banner, no workflows,
  so no CI exists.
- Progress record: [docs/OPERATOR_EXPERIENCE_V2_PROGRESS.md](docs/OPERATOR_EXPERIENCE_V2_PROGRESS.md);
  tiers and review rounds 1-7 are closed. Round 8 (below) covers the e2e/docs commits.
- Draft PR #7 (`chore/claude-gates`) adds `.claude/gates.json` and untracks
  `frontend/tsconfig.tsbuildinfo`; it asks for an e2e gate once PR #6 lands. Until then
  `gates preflight` fails with "no gates config". Never stage `tsconfig.tsbuildinfo`
  (`tsc -b` rewrites it; it shows as modified).

## Verified gates (WSL, Node v26.10.0, Python 3.12)

Run at `cb8bdd5`, before a rebase onto PR #8 (README banner). `git diff cb8bdd5 0bdaf2c`
is only `.github/assets/banner.png` and 2 README lines, so the code under test is
identical to `0bdaf2c`. Anything after `0bdaf2c` is documentation.

| Gate | Command | Result |
|---|---|---|
| Backend tests | `make test-backend` (700 tests, ~11 min) | RUN+PASSED |
| Backend lint / types | `make lint`, `make typecheck` | RUN+PASSED |
| Frontend tests | `npm --prefix frontend run test -- --run` (140 tests) | RUN+PASSED |
| Frontend lint / types | `make lint`, `make typecheck` | RUN+PASSED |
| Browser e2e | `make e2e` (6 checks, isolated, fake providers) | RUN+PASSED |

Commits after this record are documentation only. Re-run on any later code change.

## Done this session

- `4f0c07c` Test setup installs an in-memory `localStorage`: on Node 25+ the built-in
  global shadowed jsdom's, failing 8 `ActivityCommand` tests regardless of the code.
- Installed Playwright Chromium (`npx playwright install chromium`); e2e now runs.
- Review round 8 (independent `security-reviewer`, e2e harness + public docs): no
  Critical/High. Fixed M1/M2/L1-L5 in `efbdfd7` and `0bdaf2c`; L6 is PR #7.
- `ad0c3cb` Real defect found via the e2e leak: a connected UI websocket held uvicorn's
  graceful shutdown open forever, so SIGTERM/Ctrl-C never reached
  `orchestrator.shutdown()`. `server.build_uvicorn_config` now sets
  `GRACEFUL_SHUTDOWN_SECONDS = 3`; `tests/test_server_shutdown.py` fails (13 s timeout)
  without it.

## Next (ordered)

1. Push, mark PR #6 ready and merge it (no protection, no CI; gates above are the
   evidence). Then verify `main` at the merge SHA.
2. Rebase PR #7 on `main`, add `{"name": "e2e", "run": "make e2e", "timeout": 1800}` to
   `.claude/gates.json`, run `gates preflight`, merge.
3. Further work only from real dogfood evidence (`docs/dogfood/`).

## Human decisions

- Author email remains in commit metadata (`PUBLICATION.md`); acceptable for publication?
- Adding GitHub Actions CI (hosted runner minutes) is not done.

## Known gaps (documented, not built)

- On forced shutdown uvicorn logs a `CancelledError` traceback per still-open websocket
  (visible at the end of `make e2e`). Harmless; the websocket handlers could close on
  shutdown instead.
- `repair_worker` does not carry failover notes (no reliable retry-to-failed-run link
  without touching sealed Increment 4 semantics). Port only if dogfood shows duplicated work.
- Reset hints in JSON notices whose type is neither error nor fail are not read.
- The repo pins no Node version; verified on v26.10.0 only.
- `tests/conftest.py` has mypy errors under `mypy tests` (the gate checks `src` only).

## Resume

```
git status && git log --oneline -5
make lint && make typecheck && make test-frontend && make e2e
make test-backend          # ~11 min; run in the background
```
