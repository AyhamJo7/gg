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
- Every `/api/` route (`GET`/`HEAD` included, `/api/health` the sole
  exception — see Known limitations) and both WebSocket routes require a
  shared-secret bearer token, generated once per checkout and persisted to
  `.orchestrator/auth_token` (0600). There is no network route to fetch it —
  the frontend gets it out-of-band (Vite embeds it at dev/build time; the
  Tauri shell reads the file via IPC) — see `backend/src/orchestrator/api/auth.py`.
  GET was not originally covered by this check; it was widened after a
  network-enabled sandboxed subprocess (see the install-step entry below)
  was proven able to reach the orchestrator's own API and read cross-project
  data through the gap.
- The WebSocket token travels as a `Sec-WebSocket-Protocol` handshake header
  rather than a URL query parameter — a query param lands in uvicorn's
  access-log request line on every (re)connect (proven leaking the token
  into stdout at the production entrypoint's default `log_level="info"`); the
  subprotocol header does not.
- Treat the token as raising "anything on the machine can call these routes"
  to "anything that can read this user's files," not as isolating GG from
  other locally-running software — see Known limitations for what that does
  and doesn't cover.

### Planner-authored command execution (acceptance criteria, gate validation)
- Acceptance-criterion `verify` commands and external-prerequisite gate
  `validation` commands originate as LLM-authored plan text — an
  attacker-influenceable input by this tool's own threat model (prompt/task
  injection). Six remediation rounds tried to make this safe by statically
  proving a command couldn't escape the target repo, including via its own
  manifest content (go.mod `replace`, Cargo.toml `[patch]`/path deps/
  workspace members/target-conditional tables, package.json `file:`/`link:`/
  workspace deps). Two independent adversarial reviews converged on the same
  diagnosis: enumerating "which manifest fields matter" has no natural
  stopping point — every round closed a proven bypass and a new,
  unenumerated one appeared.
- The actual containment boundary is now an OS-level sandbox: these commands
  run inside `bubblewrap` (`bwrap`), confined to the target repo (read-write),
  a minimal read-only system/toolchain view, and no network
  (`--unshare-net`). A manifest directive that would have redirected the
  tool outside the repo now fails at the kernel's mount-namespace boundary
  regardless of which field caused it — the boundary doesn't depend on
  having enumerated the mechanism. See `backend/src/orchestrator/sandbox.py`.
- This includes `backend/src/orchestrator/verify.py`'s FINAL_VALIDATION/
  acceptance toolchain check: the command *string* it runs is fixed
  (auto-detected from the repo's own manifests, e.g. `"npm test"`), but its
  *behavior* is exactly as manifest-content-driven — and therefore as
  attacker/AI-influenceable — as the criterion/gate-validation paths above,
  so it goes through the same sandboxed boundary rather than being treated
  as a separately-trusted code path.
- The sandbox also unshares the PID namespace (`--unshare-pid`) and applies
  a coarse process-count cap via the shell's `ulimit` (`bwrap` has no
  `--rlimit` flag) — an earlier version shared the host's PID namespace,
  which was proven to let a sandboxed command signal-kill arbitrary host
  processes (including the orchestrator's own) despite having no filesystem
  or network access. The process-count cap is a backstop against a
  fork-bomb-shaped verify/gate command, not a precise resource guarantee; a
  real cgroup-based `pids.max`/memory limit is a tracked follow-up.
- A cheap closed-shape argv allowlist (`backend/src/orchestrator/
  criterion.py`) still runs first, as a pre-filter — it rejects obvious junk
  before a sandbox is even started, but it is explicitly **not** the safety
  boundary; do not extend it in the belief that it needs to enumerate every
  manifest indirection again.
- **Linux-only, fail-closed**: `bwrap` is not available on macOS/Windows.
  When it's missing, these commands are refused outright and reported as
  not-executable — there is deliberately no unsandboxed fallback.
- The sandbox's environment is fully cleared (`--clearenv`) except a small
  explicit allowlist (`LANG`, `LC_ALL`, `TERM`, plus toolchain-cache
  locations pointed at a fresh per-run scratch directory) — this also
  prevents any secret present in the orchestrator's own process environment
  from leaking into a sandboxed command's view. `PATH` is rebuilt from only
  the directories actually bound into the sandbox rather than forwarded
  from the host verbatim, so it can't name (even if it can't resolve into)
  unrelated host directories.
- Toolchain access under `$HOME` is an **explicit allow-list of specific
  subpaths** (`.nvm`, `.local/bin`, `.local/share/uv`, `.cargo/bin`,
  `.cargo/registry`, `.rustup`, `go/pkg/mod`, `.npm`, `.cache/uv`,
  `.cache/pip`), not the whole `.cache`/`.local` directories. An earlier
  version bound those two wholesale and relied on masking known
  credential-file names within them; that was proven insufficient on a real
  development machine (a live Hugging Face API token under
  `.cache/huggingface/token` and a live Jupyter session-signing secret under
  `.local/share/jupyter/runtime/` were both readable, since neither is a
  toolchain path any masking list had reason to name — `.cache`/`.local` are
  shared, general-purpose dumping grounds for every application on the
  machine, not toolchain-specific). The allow-list above is the actual
  boundary now; known credential files/directories (`.npmrc`, `.netrc`,
  `.git-credentials`, `.gitconfig`, `.cargo/credentials*`, `.ssh`, `.aws`,
  `.azure`, `.gnupg`, `.config/gh`, `.docker`) are still masked on top of it
  as defense in depth, not the primary control. A second, independent layer
  covers the same class of risk from the other direction: `git_ops.
  checkpoint()`'s content-based secret scan (reusing
  `security.SECRET_PATTERNS`) runs over every newly-staged file's added
  content regardless of filename, so even a credential read via some
  future unenumerated allow-list path and copied into an innocuously-named
  tracked file is excluded from the commit rather than silently persisted
  into history. **This scan covers a fixed set of known secret formats
  only — it is a heuristic, not a guarantee.** Proven live: an unlabeled
  connection string (`DATABASE_URL=postgres://user:pass@host/db`, no
  recognized label word) and a base64-encoded token both pass through
  uncaught, since there is no entropy/decode-based detection. It was also
  proven, and then fixed, that a token split across two added diff lines
  was invisible to a per-line-only scan (contiguous added lines within a
  hunk are now concatenated before matching); an AWS access-key pattern
  (`AKIA...`) was added after the same review found none existed. Treat
  this as narrowing the gap the allow-list leaves, not closing it.
- `run_sandboxed`/`build_sandboxed_argv` refuse to run at all if the target
  repo resolves to the real `$HOME` or an ancestor of it — binding such a
  repo read-write, applied after the credential masks in bwrap's bind
  order, would silently remount over and undo those masks.
- `_fresh_checkout_verify`'s dependency-install step (`npm ci`/`npm install`/
  `uv sync` on the freshly cloned, accepted SHA — see `project_engine.py`)
  runs through the same sandbox with `allow_network=True`: install
  genuinely needs a registry, so `--unshare-net` is the one confinement
  this call site can't have, but every other boundary above (PID/IPC/UTS
  isolation, `--clearenv`, the credential-masked toolchain allow-list, no
  real-`$HOME` exposure, the ulimit fork-bomb backstop, fail-closed if
  `bwrap` is missing) is identical. This was the last unsandboxed
  execution path in the codebase — proven live and now closed: a
  `pyproject.toml` declaring a malicious PEP 517 build backend could
  previously write into the real `$HOME` and see real credentials during
  this step; it's now confined to the sandbox's scratch `$HOME` and the
  credential-masked toolchain view like everywhere else. **Residual risk,
  stated plainly, not claimed as closed**: with network reachable, this
  step can still (a) exfiltrate the target repo's own content over that
  network — inherent to needing network for install at all, not fixable
  without disabling install entirely, and the repo's content isn't secret
  the way host credentials are, and (b) reach any network destination,
  since only `--unshare-net` is dropped: this is the *host's own network
  namespace*, not a scoped one, so the sandboxed install process can reach
  `127.0.0.1:<any-port>` — including, when this was reviewed, the
  orchestrator's own API. That was proven live: with `GET` routes exempt
  from the bearer-token check, a malicious install-time dependency could
  read every project's and mission's data (plans, task logs, delivery
  reports, waiver history) unauthenticated, over the same connection
  install needs anyway. **Fixed** by requiring the token on `GET` too (see
  `api/auth.py`'s module docstring) — the install step has no path to the
  real token (it operates on a fresh clone into a scratch dir; the state
  directory holding the token is gitignored and absent from that clone), so
  this closes the specific proven exfiltration path regardless of what
  `allow_network=True` can reach. What's **not** fixed: `allow_network=True`
  is still the full host network namespace, so the install step can reach
  any *other* unauthenticated local/LAN service during that window (a local
  dev database, another app's debug port, etc.) — lateral movement beyond
  this application's own API. Scoping this to registry-only traffic would
  need a private network namespace with outbound NAT and
  `--disable-host-loopback` (e.g. via `slirp4netns`, confirmed installed and
  capable of exactly this) instead of the host's namespace; this was
  evaluated and deferred rather than rushed — wiring bwrap and slirp4netns
  together correctly requires a ready-fd/FIFO handshake so the payload
  never execs before the private network is fully configured, and getting
  that synchronization subtly wrong would be a worse outcome than the
  currently-documented gap, per this document's own repeated lesson.

### Provider autonomy
- CLIs run with permission bypass flags **inside the user-selected workspace** —
  this is the point of the product, but treat mission text as code: a malicious
  task description is a prompt-injection vector against your own subscriptions.
  Use `SAFE` autonomy for unfamiliar projects (adds an approval gate before
  implementation).

## Known limitations

- The bearer token itself is not hardened against other local processes
  reading the token file — a compromised local dependency with filesystem
  access can still read `.orchestrator/auth_token` and call any route. The
  token stops a blind network caller (including, since the fix above, a
  network-enabled sandboxed subprocess with no path to the token file), not
  a co-resident attacker with file access; do not expose the port or run
  behind a reverse proxy without a stronger auth layer than this
  single-operator scheme. `/api/health` is the sole exception (any method):
  a minimal liveness probe with no project-specific data, kept open so
  scripts can detect the backend is up before the token file is guaranteed
  readable — every other route requires the token, `GET`/`HEAD` included.
- The `actor` field on acceptance waivers is a self-reported audit label,
  not a verified identity — the shared bearer token proves "holds the
  token," not "is a specific person."
- CLI sandboxing differs per provider (codex: `workspace-write` sandbox; claude/agy/
  opencode: permission-bypass in the workspace). Review generated changes in the
  Git ledger — that is what it is for.
- Full raw provider logs on disk are unredacted by design (debugging); mission
  logs live under the project's `.orchestrator/logs/`; product-planning logs
  live under the state directory's `logs/` with an isolated per-run planner
  working directory. Treat those directories as sensitive.
  `git_ops.checkpoint()` explicitly unstages the entire `.orchestrator/`
  directory; internal runtime files are also sensitive paths.
- The normal checkpoint safeguards do not cover every Git call: product workspace
  bootstrap in `ProjectCoordinator.ensure_target_repo()` directly runs `git add -A`
  when initializing an adopted non-Git directory. Existing secret files in such a
  directory can be staged by this path. Use an existing Git repository with its
  sensitive files excluded, or an empty new target, until bootstrap is corrected.
- Spawn ownership is uniform through the shared invocation boundary: every
  provider execution (sequential phases, parallel planning/tasks/review/repair,
  product planning) persists PID/PGID/start identity before the spawn gate
  releases, verifies ownership before signalling, and releases capacity only
  after confirmed exit. Invocation manifests persist sizes/hashes/estimates
  only — never raw secrets or `.env` content. Compiled-v2 block manifests
  persist per-block metadata (type, priority, representation, reason, hash)
  only; prohibited blocks are omitted before budgeting and secret-shaped
  content is dropped defense-in-depth.
- `workspace_scope` is a scheduling lock, not a filesystem sandbox: a task
  declares which paths it will touch so conflicting tasks serialize, but the
  provider process itself is not confined to those paths.
- `backend/src/orchestrator/verify.py`'s FINAL_VALIDATION/acceptance
  toolchain check now runs through the same `bwrap` sandbox as
  criterion/gate-validation commands (see above) — it is no longer a
  separately-trusted, unsandboxed code path. On a host without `bwrap`
  (non-Linux, or unprivileged user namespaces disabled), verification
  becomes unavailable rather than falling back to running unconfined; a
  mission's FINAL_VALIDATION / a product's acceptance run will report
  failure with that reason rather than silently skipping the check.
- The sandbox's read-only toolchain allow-list (`sandbox.py`'s
  `_HOME_TOOLCHAIN_SUBPATHS`) and its credential-masking defense-in-depth
  list (`_HOME_MASK_FILES`/`_HOME_MASK_DIRS`) are a fixed set of common
  locations (nvm, cargo/rustup/go, npm/uv/pip caches, ssh/aws/gh
  credentials, ...) verified against this project's own development
  machine — an unusual toolchain-manager layout on a different machine
  could need an additional subpath added (a real tool failing offline
  inside the sandbox because its cache isn't on the allow-list, not a
  security gap) or, in the credential-mask list specifically, could in
  principle miss a credential path colocated inside one of the allow-listed
  subpaths (not `.cache`/`.local` wholesale, since those are no longer
  bound at all). The content-based git-checkpoint scan described above is
  the defense-in-depth backstop for exactly this residual case.
- `bwrap` sandboxing depends on unprivileged user namespaces being enabled
  on the host kernel. If disabled (some hardened kernels/containers), the
  sandbox fails to start and — per the fail-closed design above — the
  command is refused rather than run unconfined; this can surface as
  acceptance criteria/gate validations/verification runs becoming
  permanently unexecutable on such a host rather than a security gap.
- The sandboxed process-count cap (`ulimit -u`) is a single, fixed ceiling
  applied via a `bash` wrapper, not a per-sandbox cgroup limit — it bounds
  the real UID's total process count, not just the sandbox's own subtree,
  and requires `bash` to be present (falls back to no cap, with a logged
  warning, if `bash` is missing). It stops a runaway fork bomb from
  climbing unbounded; it is not a precise or airtight resource guarantee.
  A cgroup-based `pids.max`/memory limit would be strictly better and is a
  tracked follow-up, not implemented here.
- `go` toolchain shapes (`go test`/`go build`/`go run` inside the sandbox,
  and the `GOPATH` re-pointing fix in `sandbox.py`) have no empirical
  verification in this project's own development/CI environment — no `go`
  binary is installed there. The design mirrors the independently-verified
  `CARGO_HOME`/`RUSTUP_HOME` pattern and follows Go's documented
  `GOPATH`/`GOCACHE` semantics, but should be treated as unverified until
  exercised on a machine with a real Go toolchain.
