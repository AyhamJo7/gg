# NEXT — engineering handoff

Read [AGENTS.md](AGENTS.md) first. GG is a local, single-operator orchestrator of
authenticated provider CLIs, in dogfood/stabilization: sealed Increments 1-4,
Operator Experience v1 and v2 are not reopened without evidence from a real run.

## State

- `main` contains Operator Experience v2 (PR #6, merge `62eb38d`) and the verification
  gates config (PR #7, merge `e9b08c9`). No open PRs, no open feature work.
- `main` has no branch protection and the repo has no CI workflows (`.github/` holds only
  the README banner). The gates below are the only evidence; nothing enforces them on push.
- `.claude/gates.json` lists build, lint, typecheck, unit and e2e; `gates preflight`
  accepts it. `frontend/tsconfig.tsbuildinfo` is no longer tracked.
- Progress record: [docs/OPERATOR_EXPERIENCE_V2_PROGRESS.md](docs/OPERATOR_EXPERIENCE_V2_PROGRESS.md)
  (tiers and review rounds 1-7; round 8, an independent security review of the e2e
  harness and public docs, is summarised below).

## Verified gates (WSL, Node v26.10.0, Python 3.12)

Run on commit `cb8bdd5` (since rebased as `0bdaf2c`; the code is identical, the only
difference is the README banner PR #8). `main` at `e9b08c9` differs from that tree only by
PR #7 (`.claude/gates.json`, `.gitignore`, untracked `tsbuildinfo`); `make build` was run
on the merged PR #6 tree.

| Gate | Command | Result |
|---|---|---|
| Backend tests | `make test-backend` (700 tests, ~11 min) | RUN+PASSED |
| Backend lint / types | `make lint`, `make typecheck` | RUN+PASSED |
| Frontend tests | `make test-frontend` (140 tests) | RUN+PASSED |
| Frontend lint / types | `make lint`, `make typecheck` | RUN+PASSED |
| Browser e2e | `make e2e` (6 checks, isolated, fake providers) | RUN+PASSED |
| Build | `make build` (wheel + vite) | RUN+PASSED |

Not re-run on `e9b08c9` itself: re-run the Resume block before relying on it for a release.

## Done in the last session

- Test setup installs an in-memory `localStorage`: on Node 25+ the built-in global
  shadowed jsdom's, failing 8 `ActivityCommand` tests regardless of the code.
- Playwright Chromium installed locally, so `make e2e` runs.
- Round 8 review: no Critical/High. Fixed the e2e harness (busy-port refusal, child
  liveness, bounded cleanup, `--no-install`, loopback-only `GG_BACKEND_HOST`,
  `GG_AUTH_TOKEN_FILE` only under `GG_E2E=1`), the stale `PUBLICATION.md` record
  (gitleaks findings with file, line, commit) and the author email in the dogfood report.
- Real defect: a connected UI websocket held uvicorn's graceful shutdown open forever, so
  SIGTERM/Ctrl-C never reached `orchestrator.shutdown()`. `server.build_uvicorn_config`
  now sets `GRACEFUL_SHUTDOWN_SECONDS = 3`; `tests/test_server_shutdown.py` fails
  (13 s timeout) without it.

## Next

Nothing actionable is open. Further work comes from real dogfood evidence
(`docs/dogfood/`); do not start speculative Increment 5 work.

## Human decisions

- The author email stays in commit metadata (see `PUBLICATION.md`): acceptable for publication?
- GitHub Actions CI (hosted runner minutes) is not set up; decide whether to add it.

## Known gaps (documented, not built)

- On forced shutdown uvicorn logs a `CancelledError` traceback per still-open websocket
  (visible at the end of `make e2e`). Harmless; the handlers could close on shutdown.
- `repair_worker` does not carry failover notes (no reliable retry-to-failed-run link
  without touching sealed Increment 4 semantics). Port only if dogfood shows duplicated work.
- Reset hints in JSON notices whose type is neither error nor fail are not read.
- The repo pins no Node version; verified on v26.10.0 only.
- `tests/conftest.py` has mypy errors under `mypy tests` (the gate checks `src` only).

## Resume

```
git status && git log --oneline -5
make lint && make typecheck && make test-frontend && make e2e && make build
make test-backend          # ~11 min; run in the background
```
