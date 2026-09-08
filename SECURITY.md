# Security

## Threat model

The orchestrator executes **privileged local processes** (AI CLIs authenticated
to your subscriptions) and lets them modify **arbitrary user-selected project
folders**. The primary risks: prompt/task injection steering an agent somewhere
harmful, credential leakage into logs/git, destructive filesystem or git
operations, and workspace escape.

## Safeguards implemented

### Process execution
- argv arrays everywhere; `shell=True` is banned and enforced by ruff S-rules.
- Process groups with SIGTERM→SIGKILL escalation and tree cleanup — no zombies.
- Full raw stdout/stderr captured to `<project>/.orchestrator/logs/`;
  streamed lines and events pass through redaction.

### Workspace safety
- `validate_workspace_path`: must exist, be a directory, and not be `$HOME` or `/`.
- `ensure_within` prevents path traversal for any resolved artifact path.
- Existing uncommitted user changes are checkpointed (committed, never reset)
  before a mission starts. `git reset --hard` / `git clean` are never used.

### Secret protection
- Redaction patterns (API keys, JWTs, GitHub tokens, `key=value` secrets) applied
  to every streamed line, event payload, gate detail, and run record.
- Generic-secret policy (explicit, bounded): a label (`api-key`/`api_key`/
  `token`/`secret`/`password`, case-insensitive), a `:`/`=` separator, and a
  value of 8+ non-space chars. At most 32 whitespace chars are accepted
  between label and separator, at most 256 between separator and value
  (newlines included, so YAML blocks are covered). Wider separations are
  explicitly unsupported and will not link.
- Task-log tail guarantee: the served tail keeps a 4 KB overlap for pattern
  context (derived: worst-case backward context is 8 + 32 + 1 + 256 = 297
  bytes), redacts the whole window together, and serves whole lines only
  within the 1 KB–1 MB cap — truncation can never split a supported match.
- Regex redaction is a safety net, not a substitute for keeping secrets out of
  logs: prefer environment references over inline values in task text.
- `.env*`, `*.pem`, `*.key`, `credentials.json`, `auth.json` are **force-excluded**
  from every checkpoint commit even if staged, and auto-appended to `.gitignore`.
- Provider authentication lives entirely in the official CLIs' own stores
  (`~/.claude`, `~/.codex`, …). The orchestrator never reads them.
- No browser-cookie, session-token, or keychain extraction code exists anywhere
  in this repository.

### Network
- Backend binds to `127.0.0.1` only. CORS allows the Vite dev origin and Tauri.
- Zero telemetry. Analytics are computed from the local database.
- WebSocket streams redacted lines only.

### Provider autonomy
- CLIs run with permission bypass flags **inside the user-selected workspace** —
  this is the point of the product, but treat mission text as code: a malicious
  task description is a prompt-injection vector against your own subscriptions.
  Use `SAFE` autonomy for unfamiliar projects (adds an approval gate before
  implementation).

## Known limitations

- The HTTP API has no authentication (localhost-only by design). Do not expose
  the port or run behind a reverse proxy without adding auth.
- CLI sandboxing differs per provider (codex: `workspace-write` sandbox; claude/agy/
  opencode: permission-bypass in the workspace). Review generated changes in the
  Git ledger — that is what it is for.
- Full raw provider logs on disk are unredacted by design (debugging); they live
  under the project's `.orchestrator/logs/` and are excluded from checkpoints
  by the sensitive-file rules only if they match secret filenames — treat that
  directory as sensitive.
