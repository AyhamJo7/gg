# GG Orchestrator — AI Engineering Mission Control

A local-first autonomous software factory that orchestrates multiple consumer AI
subscriptions through their locally installed CLI tools. Give it **one high-level
engineering mission**, walk away, and the system plans, implements, tests, reviews,
repairs, and verifies the work — rotating providers automatically when any of them
hits a rate limit, crashes, or needs authentication.

```
You: "Build a complete weather CLI."
        ↓
Claude plans  →  OpenCode implements  →  Codex rate-limited  →
orchestrator checkpoints + hands off  →  Claude continues testing  →
Claude reviews  →  verification engine runs real tests  →  VERIFIED COMPLETE
```

## What it actually does (verified)

- Detects your installed CLIs (`claude`, `codex`, `agy`, `opencode`) and their versions
- Drives them non-interactively with argv arrays (no shell interpolation)
- Classifies failures (rate limit / auth / crash / timeout) from real output
- Checkpoints work into Git at every phase and every provider switch
- Generates structured markdown handoffs so provider B continues provider A's work
- Enforces separation of duties (implementer ≠ reviewer when possible)
- Runs the project's real toolchain as the Definition of Done — never trusts "done"
- Recovers mid-flight missions after backend restart (SIGKILL-tested)
- Streams live output over WebSocket into a Mission Control UI

## Quick start

```bash
make install     # backend deps (uv) + frontend deps (npm)
make dev         # backend on :8787 + frontend on :5173
```

Open http://localhost:5173 → **Projects** → add a folder → **New Mission** → launch.

Other commands: `make test` (backend 46 + frontend 6), `make lint`, `make typecheck`,
`make build`, `make smoke` (real provider smoke test — consumes a little quota).

## Provider setup

All four subscriptions authenticate via their own CLIs — the orchestrator never
touches credentials:

| Provider | CLI | Non-interactive mode used |
|---|---|---|
| Claude Pro | `claude` | `claude -p <prompt> --output-format stream-json --permission-mode bypassPermissions` |
| ChatGPT Plus | `codex` | `codex exec --json --sandbox workspace-write -C <dir> <prompt>` |
| Gemini AI Pro | `agy` | `agy --print <prompt> --output-format stream-json --dangerously-skip-permissions` |
| OpenCode Go | `opencode` | `opencode run --format json --auto --dir <dir> -m <model> <prompt>` |

Verified against: claude 2.1.261, codex-cli 0.153.1, agy 1.1.26/1.1.27, opencode 1.17.13.
See [PROVIDERS.md](PROVIDERS.md) for the full adapter contract and known quirks
(e.g. opencode's interactive default model hangs headless — pin
`providers.opencode.model`).

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flow, state machine
- [DEVELOPMENT.md](DEVELOPMENT.md) — repo layout, workflows, testing
- [PROVIDERS.md](PROVIDERS.md) — adapter contract, flags, quirks, adding providers
- [SECURITY.md](SECURITY.md) — threat model and safeguards
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — common problems
- [docs/adr/](docs/adr/) — architecture decision records

## Security model (summary)

Local-first: SQLite + filesystem, no telemetry. argv-only subprocesses, workspace
path validation, secret redaction in all logs/events, `.env` files auto-gitignored
and force-excluded from every checkpoint commit. No browser-cookie or session
scraping anywhere — auth lives in the providers' own CLIs.

## Current status

Dogfooded end-to-end: a real Node.js mission ran through Claude (planning) →
OpenCode (implementation) → Codex (rate-limited mid-testing → automatic failover
to Claude) → Claude (review) → verification (real `npm test` + `npm run build`) →
COMPLETED, with git checkpoints and handoffs at every step.
