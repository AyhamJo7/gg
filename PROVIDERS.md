# Providers

## Adapter contract

```python
class ProviderAdapter:
    name: str
    executable: str

    def detect() -> (installed: bool, path: str | None)
    async def get_version() -> str | None
    def get_capabilities() -> ProviderCapabilities
    def build_command(request) -> list[str]        # argv only — never a shell string
    def normalize_output_line(line) -> str | None  # for JSON-streaming CLIs
    def extract_summary(stdout_tail) -> str        # final-message for handoffs
    async def execute(request, on_output) -> ExecutionResult
    async def interrupt(run_id) -> bool
    def classify_failure(exit_code, output, timed_out, cancelled) -> FailureClass
    async def health_check() -> ProviderState
```

Adapters translate CLI reality into normalized states. They **never** decide
orchestration policy — priority, failover, and phases belong to the engine.

## CLI integrations

Commands below reflect the current adapters. Historical runtime checks were made
on 2026-09-05; the default DB inspected on 2026-09-10 recorded Claude 2.1.267,
Codex 0.153.4, AGY 1.1.28, and OpenCode 1.17.13. Installation/version checks
are not authentication or remaining-quota checks. All four are enabled in YAML;
DB state and cooldowns affect eligibility. GG pins model/effort only for OpenCode.

### claude — 2.1.261 (Claude Code)
```
claude -p <prompt> --output-format stream-json --verbose --permission-mode bypassPermissions
```
- NDJSON stream; assistant `content[]` text is displayed; `result` event carries
  `subtype:"success"` and the final text.
- Emits `rate_limit_event` telemetry mid-stream (status `allowed`/`allowed_warning`)
  which is **not** a failure — the classifier is success-aware for this reason.

### codex — codex-cli 0.153.1
```
codex exec --json --sandbox workspace-write --skip-git-repo-check --color never -C <dir> <prompt>
```
- JSONL events; `turn.failed` with "usage limit" text → RATE_LIMIT (verified live
  during the dogfood mission; failover triggered correctly).

### agy — 1.1.26 / 1.1.27
```
agy --print <prompt> --output-format stream-json --dangerously-skip-permissions --print-timeout <N>m
```
- No cwd flag exists; process cwd is set instead.
- Result event: `{"event":"result","result":{"status":"SUCCESS",...}}`.

### opencode — 1.17.13
```
opencode run --format json --auto --dir <dir> -m <model> <prompt>
```
- **Quirk (verified):** the interactive default model (`gpt-5.6-sol-pro` on this
  machine) hangs indefinitely in headless `run` mode with zero output. The adapter
  pins `providers.opencode.model` (currently
  `opencode-go/muse-spark-1.3-contributor`) and passes `--variant xhigh` from
  configuration. Change these in `config/orchestrator.yaml` for your account.

## Failure classification

Patterns in `providers/classify.py` map output tails + exit codes to:

```
RATE_LIMIT · QUOTA_EXHAUSTED · AUTH · OVERLOADED · TIMEOUT · CRASH ·
MALFORMED_OUTPUT · HUMAN_INPUT · CANCELLED · NONE
```

Terminal precedence is deterministic: confirmed cancellation, timeout, spawn
refusal, and explicit terminal failure evidence take precedence over any
intermediate success-like marker. A non-zero process exit is failure even when
an earlier `item.completed`/`step_finish`/`result` event exists. Do not infer
successful task acceptance from `failure_class=NONE` alone; see invocation
outcome and acceptance evidence.

## Usage visibility

Every provider execution is one durable invocation (`InvocationService`) with a
prompt manifest (`char4-v1` local estimate, explicitly approximate) and a
normalized usage row carrying source/completeness/basis. `null` means unknown,
never zero.

- Claude: final `result.usage` is authoritative (`PROVIDER_REPORTED`); `modelUsage`
  is corroborating breakdown only. Missing terminal usage stays `UNKNOWN`/`PARTIAL`.
- OpenCode: unique `step_finish` deltas are summed; duplicates ignored; unfinished
  runs are `PARTIAL` lower bounds.
- Codex: terminal stdout usage when present; cumulative session snapshots use the
  final snapshot only, never summed. Unattributable sessions stay `UNKNOWN`.
- AGY: `UNKNOWN` until a verified fixture proves a schema.

CLI-reported usage is not independently measured provider billing. GG prompt
estimates exclude CLI-added instructions and internal tool turns. Subscription
quota remaining is unknown unless separately exposed with trustworthy provenance.
Requested vs observed model are stored separately; unknown stays null.

## Simulation providers (tests/development)

`providers/fake.py` — `FakeAdapter(name, script)` where script tokens per call:
`ok`, `work` (writes real file), `ratelimit`, `crash`, `auth`, `slow`
(interruptible). Used by the whole E2E suite; they never touch the network.

## Optional bridges & LiteLLM

The architecture tolerates OpenAI-compatible local bridges (e.g. LiteLLM via
`docker compose --profile litellm up -d`) but **nothing depends on them**.
No browser-cookie proxies are used or supported: authentication lives in the
official CLIs. Any future bridge adapter must be optional, env-var configured,
and documented here with its ToS implications.
