# GG architecture audit and improvement blueprint

Date: 2026-09-10. Source baseline: `6ebcc7d` on `main`.
Status: **assessment and proposed design; no product implementation in this pass**.

This report uses current source, nine SQL migrations, the default local database,
its associated provider logs, one precisely matched Codex session, local CLI help,
and deterministic tests. Historical release reports were inspected as history,
not accepted as proof of current behavior. Paths/line numbers below refer to the
source baseline. No real provider jobs or new dogfood products were launched.
No unrelated provider conversations or credential stores were inspected.

## A. Executive assessment

GG is a functioning local orchestration workbench with a substantial mission
engine and a newer product coordinator. It can plan products, operate repositories
through subscription CLIs, checkpoint work, run parallel task branches, record
reviews, stop at human prerequisites, and attempt evidence-based delivery. This is
more mature than a prompt-chain prototype, but less uniform than the UI's single
product lifecycle suggests.

Its strongest parts are the real Git ledger, explicit terminal states, bounded
mission repair, process-identity recovery work, deterministic fake-provider tests,
separation between work completion and requirement acceptance, and Linux sandbox
containment for objective checks. Retain these foundations.

Its weakest part is **the contract between stages**. Product planning bypasses
mission execution accounting; architecture disappears from phase prompts;
parallel dependencies order execution without transferring their code; review
provenance misses later writers; acceptance evidence does not fully bind the
verified behavior to the delivered checkout. These gaps waste more capacity than
verbose prompt headings alone.

The single biggest bottleneck is the absence of a consistent, observable contract
connecting **intent → invocation → artifacts → independently checked evidence**.
GG cannot reliably explain what a model knew, why its run was classified as
successful, or whether that exact result satisfied the requested behavior.

Build a shared durable invocation boundary first, then a deterministic context
compiler and stronger acceptance evidence. Do not add an LLM Prompt Engineer to
every invocation. That would consume quota while leaving these broken contracts
in place. Do not rewrite the backend into services: SQLite and one local process
remain appropriate.

No universal P0 is assigned: working workflows exist and the audit did not prove
that every normal project is blocked. Several P1 correctness issues must precede
expanding unattended autonomy.

## B. Current architecture map

```text
Browser HashRouter: Idea → Product             Existing repo → New Mission
  LifecyclePage / LifecycleDetailPage          NewMissionPage / MissionControl
                 │                                      │
                 └────────────── REST ──────────────────┘
                         api/app.py + api/auth.py
                                  │
       ProjectCoordinator         │           Orchestrator
       product_plan.py schema     │           scheduler + workspace owner
          │                       │                 │
          ├─ direct planner CLI   │        ┌────────┴─────────┐
          │  (special path)       │        │                  │
          └─ roadmap phases ───────── MissionEngine   ParallelMissionEngine
                                     sequential       DAG/readiness
                                         │            reservations/locks
                                         │            task worktrees
                                         │            final integration
                                         └────────┬─────────┘
                                           review / repair
                                           verify.py
                                                │
                          ProviderRegistry → adapters → process.py
                                                │
                                     spawn gate → local CLIs

   Product completion → criteria/gates/findings → fresh clone toolchain → DELIVERED
   Shared: SQLite, EventBus, Git, sandbox, redaction, raw local log files
```

### Important sources and boundaries

| Area | Source / important symbols | Actual responsibility |
|---|---|---|
| Startup/config | `server.py:26,45`; `config.py:13`; `Makefile` | Builds DB/registry/orchestrator; overlays persisted role priorities; starts uvicorn |
| API/auth | `api/app.py:100`; `api/auth.py` | REST, lifecycle/actions, mission/global WS; shared bearer token, health exception |
| Scheduler | `orchestrator.py:185` `_scheduler_loop` | Wake engines, reconcile health, await product advancement, launch workspace/provider waiters |
| Mission lifecycle | `orchestrator.py:221` onward | Launch by scheduling mode, pause/resume/cancel, retry and recovery |
| Sequential engine | `engine.py:311,667` | Provider phases, handoffs, bounded failover/review repair, objective final validation |
| Product lifecycle | `project_engine.py:82` `ProjectCoordinator` | Plans, phases, gates, latest-attempt mission links, criterion evidence and delivery |
| Parallel engine | `parallel_engine.py:346,1214` | Readiness, reservations, task branches; final integration then review/verify |
| DAG | `dag.py:21,99`; `readiness.py:170` | Validation, ID namespacing, topological readiness and scope conflict filtering |
| Reservations | `reservations.py:47,107`; `task_locks.py:23` | Priority/success scoring, task-scoped reservation records and locks |
| CLI boundary | `providers/base.py:25,31,42,141`; `process.py:82` | Minimal capabilities; prompt request; normalized result; subprocess streaming |
| Recovery | `_spawn_gate.py`; `orphans.py`; `parallel_engine.py:160` | Identity-before-exec where callbacks are supplied; reconcile interrupted runs/tasks |
| Git/integration | `git_ops.py:209`; `task_worktree.py:36`; `integration.py` | Secret-safe checkpointing, worktrees, branch merges and conflicts |
| Review | `review.py:24,104,165,258` | Instructions, marker parsing, fingerprints, explicit resolution and blocker lookup |
| Verification | `workspace.py:47`; `verify.py:89`; `criterion.py` | Detect tools; run deterministic checks; allowlist criterion commands |
| Containment | `sandbox.py:179,310`; `security.py` | bwrap mounts/network policy, environment clearing, path validation/redaction |
| Events | `events.py:60,89,93` | Durable structured events; transient provider output and bounded replay |
| Frontend | `frontend/src/main.tsx`, `pages/*`, `components/*`, `lib/*` | Hash routes, polling, mission WS, operational cards, manual DAG editor |

Paths in this table are relative to `backend/src/orchestrator/` unless qualified.
The root [current architecture](../ARCHITECTURE.md) is the concise navigation map.

Direct source entrypoints: [product coordinator](../backend/src/orchestrator/project_engine.py),
[sequential engine](../backend/src/orchestrator/engine.py),
[parallel engine](../backend/src/orchestrator/parallel_engine.py),
[provider contract](../backend/src/orchestrator/providers/base.py),
[failure classifier](../backend/src/orchestrator/providers/classify.py),
[product schema/prompts](../backend/src/orchestrator/product_plan.py),
[process runner](../backend/src/orchestrator/process.py),
[API](../backend/src/orchestrator/api/app.py),
[lifecycle UI](../frontend/src/pages/LifecycleDetailPage.tsx),
[Analytics UI](../frontend/src/pages/AnalyticsPage.tsx).

### Data model and infrastructure

Nine migrations create/extend 26 domain tables plus `schema_migrations`. Repository
`projects` own missions; `product_projects.target_project_id` optionally points to
a registered repository. Product plans are immutable revisions. Phase rows are
mutable and each points to its latest mission. Missions own tasks, runs, reviews,
findings, handoffs and gates. Dependencies/reservations/locks/branches/integrations
support DAG execution. Criterion results are overwritten per product/criterion;
waivers are bound to content hashes. There is no invocation usage or context table.

SQLite uses WAL, foreign keys, one connection and an RLock. Most CRUD calls commit
individually. The project advancement lock serializes across all products, not just
one product. This is a single-process local design; there is no durable cross-process
owner lease. Do not add tenant columns or cloud infrastructure to solve local
ownership bugs. Product IDs and canonical workspace IDs are still essential
isolation boundaries for retrieval, evidence and analytics.

Runtime requires local CLIs, filesystem/Git and bwrap for verification. No Redis,
queue broker or gateway is required. The optional LiteLLM Compose profile is not
in the execution path. Tauri is a thin shell; it inherits cwd for backend state,
attempts startup and a health wait, then loads even after an unsuccessful wait.
Its child kill is not a graceful orchestrator shutdown protocol.

## C. Actual idea-to-delivery flow

The default DB has **zero product projects**, one sequential mission, nine
provider runs, 117 events and 14 findings. Therefore the full product lifecycle
below is a **current-code trace**, exercised by deterministic lifecycle tests,
not an invented historical delivered product. The observed mission is described
separately in E.

| Stage | Code / state / provider / context | Retry, failure, human interaction and output |
|---|---|---|
| 1. Idea form | `LifecyclePage.create`; `POST /api/product-projects`; `create_project:97` writes DRAFT with idea/constraints/path/preferences | Rejects blank fields/invalid explicit paths. No provider. Creates product only, not plan |
| 2. Generate Plan | `LifecycleDetailPage` → `POST .../{id}/plan` → `generate_plan:333`; state PLANNING | Long HTTP request. Planning role priority currently starts with Claude. No durable planner operation or invocation record |
| 3. Planner execution | `_run_planning_provider:382`; cwd is shared system temp directory; `build_planner_prompt:233` | Sends idea/constraints/schema/command rules. No target-repo inspection; workspace hint defaults to fresh repo. Uses adapter directly. A returned failure is erroneously counted successful; thrown errors record failure and propagate |
| 4. Validate/repair plan | `extract_product_plan`, `validate_product_plan`; MAX_PLAN_ATTEMPTS=3 | Malformed plans get a repair prompt with errors and only first 6,000 chars of previous output. Valid JSON becomes immutable `plan_revisions`; state PLAN_READY. Exhaustion BLOCKED |
| 5. Start/approve | `start_project:581`, `ensure_target_repo:509`; repo registration + phase/evidence/gate rows | Explicit Start is the practical approval. `require_plan_approval` is stored but not enforced; PLAN_READY is absent from scheduler's active-product query, so auto-execute is not reliably reached automatically |
| 6. Provision | Existing target adopted or new repo created. Default configured roots cause fallback to `~/projects/gg-products/...` | Creates README from idea and initial Git commit. Existing non-Git folder bootstrap uses direct `git add -A`, outside normal secret-safe checkpoint helper; fix before broader adoption |
| 7. Advance roadmap | `advance_all:663`, `_advance_phase_locked:720`; dependencies/gates determine launchability | Shared asyncio lock; same repo missions serialize through workspace ownership. Waiting prerequisites allow independent phases to proceed where possible |
| 8. Create phase mission | `_phase_prompt:794`, `_launch_phase_mission_locked:822`; phase.mission_id set; mission BALANCED/balanced | Includes phase task list, phase criteria and suggested verify commands. Omits architecture, mapped requirement bodies, dependencies' handoffs, deliverables/scopes/provider hints. Does not select PARALLEL_SAFE |
| 9. Mission analyze | `MissionEngine.run:667`, `_phase_analyze:709`; workspace/toolchain and Git state | Initializes repo if needed, ensures ignore rules, handles detached HEAD, checkpoints pre-existing changes. Persists current phase; no model needed |
| 10. Mission plan | `_run_provider_phase:311`, `_build_prompt:595`; task/run/handoff records | A second planning invocation per roadmap phase. It requests an actionable plan, but downstream handoff keeps only a 200-character summary. Provider summary itself is capped at 2,000 chars |
| 11. Implement and test | Sequential phases use role priorities: OpenCode implementation; Codex testing by default | Same workspace, fresh CLI process each call, no resume/session flags. Testing role may modify code. Four configured provider attempts per phase, waits bounded at 7,200s. Auth/input can open mission gates |
| 12. CLI execution | Adapter builds argv → `run_process`; mission callback persists PID/PGID/start before gate release | Raw files plus redacted display; timeout/cancel uses process groups. Result and Git heads recorded. Success checkpoints; failure class drives cooldown and switch. Not every alternate execution path supplies this ownership contract |
| 13. Review/repair | `_phase_review_loop:854`, `review.py`; reviewer different from last IMPLEMENTATION provider where possible | Structured findings with severity/fingerprint. Open BLOCKER/HIGH findings trigger up to three repair cycles; unresolved/unparseable outcomes can be UNVERIFIED. Reviewer sees prior short agent summaries. Repair omission does not prove resolution |
| 14. Final validation | `_phase_final_validation:911` → workspace detection → sandboxed tests/lint/typecheck/build | No LLM verifier. Unknown tools or failure yield UNVERIFIED. Recognized environment-only failures skip code repair. Other failures can spend remaining repair cycles; this repair path does not re-review |
| 15. Phase completes | `_evaluate_phase_mission_locked:863`; COMPLETED evidence and requirement WORK_COMPLETED | Phase-level acceptance commands are not separately executed here. FAILED/UNVERIFIED mission may restart the whole phase up to two attempts; cancellation requires operator retry. Old mission link is replaced |
| 16. Product review | `_recompute_project_state_locked:1028` sets REVIEWING when all phases terminal-success | This is a transition label; no additional product-wide LLM review runs. Next advance moves to FINAL_ACCEPTANCE |
| 17. Acceptance | `_run_acceptance_locked:1405`; gates, working tree, checkpoint, toolchain, criterion results, findings | Requirement commands are executed, except cached command/SHA matches (including failures). Relevant MEDIUM-or-higher findings also block, unlike the mission repair threshold. No criterion-specific repair task is created |
| 18. Fresh checkout | `_fresh_checkout_verify:1494`; clone recorded SHA, install deps, run detected toolchain | npm ci/install with ignore-scripts or uv sync --frozen; network enabled only for install. Does not replay requirement-specific criteria. Fails BLOCKED on reproduction errors; scratch checkout removed |
| 19. Delivery | `_build_delivery_report:1566`; delivery_sha/report + DELIVERED state | Records stack, requirements, criteria, phases, gates, waivers, run guidance. Recent toolchain events currently come from a global query, not this product alone. No built-in app launcher or validated executable run recipe |

Parallel Safe is an **alternative mission path**, not a stage inserted automatically
into every product phase. It plans/accepts a DAG, checks readiness, reserves providers,
locks scopes, creates worktrees, runs tasks, then integrates completed branches.
Dependency completion does not currently imply dependency code visibility in the
next worktree. After integration it reviews/repairs and verifies. It neither shares
all sequential retry semantics nor streams all task output through the same path.

Information is copied at idea→README, phase prompt→mission.task, mission.task→every
handoff→prompt, summary→completed-work handoff, findings→handoff plus additional
context, and verification failures→repair prompt. Full roadmap copying is not the
dominant current issue. Several transfers instead lose necessary information.

## D. Prompt architecture audit

### Builders and what actually reaches the CLI

| Prompt/context | Current producer | Finding |
|---|---|---|
| GG role/system-like instructions | `engine.ROLE_PROMPTS:72`, `review.REVIEW_INSTRUCTIONS:24`, parallel `_build_prompt:791` | All concatenated into a CLI user prompt; GG does not use a distinct system-message channel |
| Product planner | `product_plan.build_planner_prompt:233` | Detailed 4,002-char minimal template; stack/simple-MVP constraints and allowed command shapes. No provider/toolchain inventory or existing-repo facts |
| Plan repair | `build_plan_repair_prompt:312` | First 6,000 characters only; a long plan's broken tail and valid requirements can disappear. Fresh invocation lacks original idea/full schema |
| Sequential task | `engine._build_prompt:595` + `render_handoff:15` | Full mission text, workspace metadata, bounded summaries/tests, blocker list, Git HEAD, repeated role instructions |
| Phase context | `project_engine._phase_prompt:794` | Already projected, but architecture/mapped requirements are missing |
| DAG planner | `parallel_engine._build_planning_prompt:1136` | Requests title/role/dependencies/scope/provider; schema example omits description and acceptance. No bounded format-repair despite a retry comment |
| DAG implementer | `_build_task_prompt:1485` | Title/description and generic rules only. No mission intent, dependency output, scope, acceptance, or role-specific review contract |
| Review | `REVIEW_INSTRUCTIONS`, `_prior_findings_context` | Good structured findings and explicit resolution intention; poor diff/requirement anchoring, repeated contract, prior-agent self-summary contamination |
| Repair | Generic fix instruction + open blockers or failure tails | Whole original mission still present; open findings duplicated in handoff/extra context. No structured failing-requirement contract |
| Verifier | `verify.py`, `criterion.py` | Deterministic; no prompt needed |
| Coordinator | State machine | No LLM coordinator today; preserve this |
| Git/code | Workspace summary + current HEAD | No scoped diff/code retrieval in the builder; CLIs discover code themselves |
| Instruction files | `workspace.INSTRUCTION_FILES` lists names only | File bodies are not injected by GG; CLIs can independently load project/global instructions |
| Logs/dependencies | Short test/failure summaries; no dependency handoff protocol | No evidence of GG injecting entire raw logs into every prompt. Parallel dependency outputs are absent, not overlarge |

The sequential role instruction appears twice in the handoff (`Current goal`,
`Next exact action`) and a third time under phase instructions. Review's 1,076-char
contract therefore has 2,152 avoidable duplicate characters per sequential review.
The universal “end with a one-paragraph summary” also competes with exact structured
planner/reviewer output contracts. Define one role-specific output contract instead.

There is no explicit model field in `ExecutionRequest`, no context limit in
`ProviderCapabilities`, and no usage in `ExecutionResult`. Only OpenCode's adapter
constructor receives configured model/variant. No invocation version/hash/manifest
exists. Sequential tasks save only the **last 4,000 prompt characters**. Run argv
records replace long arguments with `<prompt N chars>`. Parallel paths persist
different subsets of run metadata. A future inspector cannot truthfully reconstruct
every historical prompt from these fields.

Review independence currently means different **provider name from the last
implementation run**, not different author of the reviewed code. It excludes
neither TESTING-role edits nor all repair writers/all DAG implementers. Strengthen
provenance before displaying model-effectiveness rankings.

## E. Context and token waste analysis

### Observed sample and measurement method

Read-only snapshot: `backend/.orchestrator/orchestrator.db`; mission
`44cdae6dbf4d45c5`, nine sequential runs on 2026-09-10. The mission is UNVERIFIED;
its stored failure references a corepack/DNS environment problem. Current HEAD
already includes changes to this environment-repair behavior, so this history is
**not proof that the same failure still spends repair cycles today**.

Character counts below come from stored argv placeholders. Words come from
reconstruction using persisted handoffs, current role templates, and surviving
additional-context tails. Eight of nine reconstructed lengths match the recorded
length; this corroborates size, not an unavailable historical hash. The third
review cannot be fully reconstructed. Tokens use `ceil(characters/4)`: an explicit
rough local estimate for the GG-authored string, not actual model input accounting.

| Invocation | Recorded chars | Reconstructed words | Estimated GG prompt tokens |
|---|---:|---:|---:|
| Mission planner, Claude | 14,374 | 1,954 | ~3,594 |
| Implementer, OpenCode | 14,485 | 1,934 | ~3,622 |
| Testing, Codex | 14,546 | 1,945 | ~3,637 |
| First review, Claude | 17,847 | 2,342 | ~4,462 |
| First repair, OpenCode | 16,671 | 2,197 | ~4,168 |
| Second review, Claude | 20,536 | 2,596 | ~5,134 |
| Second repair, OpenCode | 17,961 | 2,345 | ~4,491 |
| Third review, Claude | 22,668 | unknown complete word count | ~5,667 |
| Final repair, OpenCode | 17,847 | 2,247 | ~4,462 |
| Product planner template with literal `idea`, no constraints (synthetic) | 4,002 | 513 | ~1,001 |

There is no recent product-plan invocation or real product plan in this DB to
measure. Do not substitute synthetic fixture plans for real generated-plan quality.
The 200,000-byte extraction limit is a safety cap, not a recommended plan size.

### Quantified observations

- The original mission is 12,527 characters and is resent in all nine prompts:
  112,743 of 156,935 total prompt characters, **71.8%**. This is repeated context,
  **not a claim that 71.8% is unnecessary**. Each fresh role still needs relevant
  requirements; the improvement is scoped projection and preserving testable IDs.
- Three reviews repeat the role contract unnecessarily: 6,456 characters total,
  roughly 1,614 estimated tokens. Removing this is safe and cheap but insufficient
  compared with CLI-internal exploration.
- First-to-third review prompt grows from 17,847 to 22,668 chars (27%). Prior
  findings/summaries cause growth; no relevance/budget policy explains it.
- The sampled target currently has a 17,852-character AGENTS.md and a
  15,689-character CLAUDE.md (~4,463/~3,923 estimated tokens). These were inspected
  for sizes only. GG did not inject their bodies itself. Actual CLI inclusion,
  global instructions and edits since each run cannot be inferred from size alone.
- Fresh invocations omit explicit session reuse. Repetition may still benefit
  from provider prefix caching; deleting repeated input from a new session would
  lose information. Content hashes alone do not create model memory.
- The sequential planner's actionable plan is reduced to a 200-character handoff
  entry. This is **lossy under-context**, despite paying for a planning call.
- The first OpenCode implementation has 40 step-finish usage events and a
  458,434-byte raw log. GG's 14,485-char prompt is only the initial request;
  internal tool turns account for much more usage.
- The first Claude review's final result reports 92 uncached input tokens,
  113,923 cache-creation input tokens, 4,549,613 cache-read input tokens and 22,763
  output tokens, of which 12,736 are thinking tokens. These are reported counters,
  not a 4.7-million-token context window or subscription charge.
- The precisely matched Codex testing session reports 13,792,745 cumulative input
  tokens (13,508,352 cached), 36,496 output (11,511 reasoning), total 13,829,241.
  Its last token event is within the GG run interval. GG's own stdout log has no
  turn-final usage event. This is why both usage completeness and provenance matter.

The largest opportunity is fewer unnecessary CLI turns and repeated whole-stage
reruns: better task/architecture handoffs, precise code/criterion evidence, environment
preflight, and smaller repair scope. A shorter wrapper alone cannot promise any
particular percentage reduction in subscription consumption.

## F. Proposed context compiler

### Decision: deterministic core, optional refinement later

Choose **C, a hybrid architecture whose normal path is A**. Deterministic selection,
budgeting, validation and rendering handle every invocation. An optional LLM may
help resolve a genuinely ambiguous product brief or compress an unusually complex
handoff, but is a visible, budgeted operation with its own outcome evidence. Never
insert a mandatory “Prompt Engineer” call in front of each coding/review call.

GG already owns structured requirements, phases, findings, task dependencies and
Git references. Most missing context can be selected directly. Deterministic
compilation is cheaper, testable, reproducible, and cannot silently rewrite
acceptance requirements. An extra rewriting model lacks code evidence unless GG
supplies it anyway, and adds another failure/availability dependency.

### Inputs, outputs and integration

Proposed `ContextRequest`: owner IDs, role, current task/objective, plan revision,
selected requirements/criteria, architecture references, allowed scope, base/head
SHAs, dependency artifact IDs, current findings, failure/evidence IDs, selected
provider/model profile and policy version. No raw credential values.

Proposed `ContextBlock`: stable ID, semantic kind, scoped source reference/version,
content hash, original size, selected representation, selected size/estimate,
priority, sensitivity, trust level, and selection reason. Runtime content is
separate from persisted metadata. Required context is validated before rendering.

Proposed `CompiledContext`: prompt string, prompt hash, template/policy versions,
input estimate/method, budget decision, blocks manifest, warnings. The execution
request receives this instead of independently composed strings. The coordinator
still decides what to do; adapters still translate CLI protocols.

Integration points: `build_planner_prompt`, `build_plan_repair_prompt`, product
`_phase_prompt`, sequential `_build_prompt`/`_make_handoff`, parallel planning,
parallel task prompt and final review/repair. Extract one compiler, not another
parallel engine. A compatibility policy initially reproduces existing prompts for
instrumentation; switch each role after fixture comparisons.

### Hierarchy and priorities

L0–L8 is a useful **classification**, not a rule that all low-numbered layers win.
A current failing assertion in L7 can matter more than an old architecture
rationale in L2. Stable source/requirement IDs are more useful than a long global
narrative. Use explicit priority and role policy:

| Layer | Normal policy |
|---|---|
| L0: GG behavior | MUST: role authority, safety boundaries, stop condition and one output contract; no repeated full agent manual |
| L1: product summary | SHOULD: durable objective, users, hard constraints, non-goals; exclude unrelated roadmap details |
| L2: architecture | MUST for affected contracts/security decisions; SHOULD for relevant rationale; retrieve other ADRs |
| L3: phase | SHOULD: goal/deliverables and current boundary; summary only of preceding phase |
| L4: task | MUST: exact task, allowed scope, mapped requirements, acceptance, unknowns |
| L5: dependencies | MUST for required interface/artifact contracts; optional short work summaries; never full transcripts |
| L6: code/diff | MUST for reviewer evidence access; include relevant hunks within budget; otherwise path/symbol/SHA references and retrieval |
| L7: findings/errors | MUST for repair target and active regression findings; unrelated/closed history excluded |
| L8: raw logs | RETRIEVE ON DEMAND, bounded redacted slices; include if necessary to understand failure; never entire logs by default |

Priority classes: MUST_INCLUDE, SHOULD_INCLUDE, IF_BUDGET, SUMMARY_ONLY,
RETRIEVE_ON_DEMAND, NEVER_INCLUDE. Mandatory overflow triggers a task-split or
larger eligible-model decision; never silently drop R4 to fit a prompt. Content
that cannot be safely disclosed is rejected before priority selection.

### Role-specific packing policies

| Role | Mandatory | Optional/retrieval | Excluded | Maximum useful history |
|---|---|---|---|---|
| PLANNER | Idea, constraints, existing-vs-new repo facts, supported environment/command contract, schema | Compact repo map, available stack recipes, already-approved decisions | Raw logs, past implementation narratives, unrelated product data | Current approved plan plus one failed structured-output attempt |
| ARCHITECT | Product boundaries, NFR/security constraints, affected interface/data decisions, environment support | Targeted modules/manifests, relevant ADR rationale | Full run transcript, routine task logs | Current architecture plus directly superseded decisions needed to explain migration |
| IMPLEMENTER | Task, scope, mapped requirement text/checks, affected architecture contracts, dependency artifact references | Relevant code/symbols, diff since base, one targeted failure tail | Entire roadmap, unrelated dependencies, closed findings | Direct dependency handoffs and latest attempt; no chronological project history |
| REVIEWER | Objective/criteria, base and head, changed-file map, relevant contracts, independent test evidence | Surrounding code, affected dependency interfaces; prior unresolved findings in a clearly separate regression section | Implementer reasoning, confidence, “all done” self-assessment, provider preference narratives | Current delta plus prior unresolved findings and latest repair delta |
| REPAIRER | Defect/criterion ID, observed vs expected result, failing command, relevant evidence, affected files, latest SHA | Minimal architecture context, failed reproduction tail, previous repair's change summary | Other project history, unrelated findings, full roadmap | Current defect and one previous repair attempt; older attempts as outcome counters/references |
| VERIFIER | Exact SHA, command/check IDs, environment recipe, expected assertions and limits | Machine-readable setup facts | LLM history and prompts | Current candidate only; same environment/check identity for cache reuse |
| PROJECT COORDINATOR | Full canonical plan and durable states/evidence, attempt/repair budgets | Aggregate diagnostics and phase summaries | Raw provider transcripts | Full durable history remains queryable, but no LLM prompt is required |

ARCHITECT is a proposed optional planning operation, not a currently implemented
role or an obligatory extra stage for trivial projects. A verifier stays
deterministic; using an LLM to summarize failed checks must not let it decide pass.

Reviewer independence has two dimensions: different writer/reviewer provenance
and an evidence-focused context. For first review, supply code/requirements before
implementation commentary. For rereview, a labeled regression section identifies
prior unresolved findings, but does not replace a fresh scan of changed code.
Track all writers after the base SHA, including TESTING and REPAIR, and disclose
provider/model/session overlap separately. Do not request or store private
chain-of-thought. Structured change/evidence summaries suffice.

### Canonical prompt structure

Standardize the following sections, omitting empty optional sections rather than
rendering boilerplate. Do not append a generic contract after a role-specific one.

```text
ROLE AND AUTHORITY
OBJECTIVE / CURRENT TASK
REQUIRED BEHAVIOR AND ACCEPTANCE IDS
RELEVANT PROJECT / ARCHITECTURE CONTRACTS
DEPENDENCY ARTIFACTS (files, interfaces, SHAs)
FILES / ALLOWED SCOPE / BASE AND HEAD
CURRENT EVIDENCE OR FAILURES
CONSTRAINTS / UNKNOWNS / STOP CONDITIONS
VERIFICATION COMMANDS AND EXPECTED OBSERVATIONS
OUTPUT CONTRACT
```

The headings, required metadata, evidence trust labels and contracts are versioned
templates. Scope, files, criteria, architecture and failure evidence are dynamic.
Advertise only tools actually available in the selected CLI/workspace, not invented
GG retrieval tools. An output contract for implementation is a compact artifact
handoff (changed files, tests/evidence, outstanding obligations); review returns
findings/resolutions; planning returns a validated plan. None requests reasoning
transcripts. Unknown or contradictory mandatory requirements trigger a structured
blocked result, not the universal current instruction to never ask questions.

### Selection and budget algorithm

1. Load a consistent owner/revision/SHA snapshot; resolve only same-product
   sources and canonical in-repo paths. Reject symlink escapes, secret paths,
   stale source references and untrusted instructions masquerading as policy.
2. Construct semantic blocks from structured records. Deduplicate by scoped
   source ID/hash; do not merge distinct requirements because prose looks similar.
3. Apply role exclusions, select current open findings and direct dependencies.
4. Add mandatory blocks. Estimate with a locally available, validated tokenizer
   for the actual model when possible; otherwise version the char-based estimate.
5. Fill remaining budget in priority order with stable tie-breaks: relevance to
   required IDs, current SHA, source order. Prefer complete short blocks and
   structured summaries to mid-sentence truncation.
6. Summarize lower-priority blocks deterministically (fields/changed symbols/
   error codes); otherwise omit with a resolvable evidence reference. Truncate logs
   around the error, not merely the first N characters.
7. Validate coverage again, render one role-specific output contract, estimate the
   rendered prompt including wrappers, and persist the manifest before execution.

Budget formula when a trusted effective context limit is known:

```text
available_input = effective_context_limit - reserved_output - safety_margin
GG_prompt_budget = min(operator_role_budget,
                       available_input - known_CLI_instructions - tool_headroom)
```

The adapter currently knows none of these budgets. Add optional capability
metadata with source/model/version/observed time; operator overrides are explicit.
Claude history reports contextWindow=1,000,000 and maxOutputTokens=64,000 for one
observed model; Codex's associated session reports model_context_window=258,400.
Neither is a universal provider limit or a safe default for future runs.

If context capacity is unknown, enforce a configured **GG prompt soft budget**
and label total CLI headroom unknown. Start budgets by role from measured prompt
distributions and operator constraints; do not assert a guessed provider maximum.
Reserve ample internal tool-turn headroom: GG cannot ensure an autonomous CLI's
entire session remains within the initial prompt budget. Calibrate estimates
against first-turn input where it is actually reported, not cumulative run input.

Context window is simultaneous working capacity. Subscription quota is a provider's
account/time-window allowance, often opaque and not proportional to the visible
token counters. Treat them as separate systems and UI concepts.

### Compression and bounded memory

Keep the full plan in `plan_revisions`. Build task projections without another
LLM call. Durable product/architecture memory is a versioned view of approved
requirements and ADRs, not a running chat summary. Phase handoffs contain artifacts,
interfaces, verified commands and remaining obligations. Finding memory is active
IDs plus evidence links; Git memory is base/head/diff, not the whole repository.

Dependency handoffs should be structured: producer task, artifact commit, exported
interfaces/files, verification IDs, limitations, consumers. The compiler may say
“read this file at this commit” only after the dependency code is actually present
in the consumer workspace. Fixing prompts cannot replace that DAG integration fix.

Hashes detect unchanged blocks and enable manifest deduplication/metrics. They
do not justify sending only deltas into fresh CLI sessions. Session reuse is a
later experiment limited to the same task/role/provider/model/worktree/trust
boundary, with recorded session ID and checkpoint. Never reuse the implementer's
session for independent review. Compare consumption and output quality before
making session reuse automatic.

### Context persistence and security

Smallest useful manifest: one row per invocation with template/policy versions,
safe prompt hash, estimated tokens/method, total original/included bytes and
ordered block descriptors. Each descriptor needs kind, scoped source reference,
source version/hash, original/included size, priority, decision
(included/summarized/omitted), and reason. Use JSON for this list initially;
separate block tables only when aggregation needs justify it.

Persist safe rendered text only under a bounded, explicit retention policy. Default
to metadata and allowlisted redacted snippets sufficient for the inspector. A
source path is not a snapshot: immutable source versions are necessary to explain
past context. Never persist `.env`, auth files, raw tool arguments, environment
values or provider secrets. Hash only approved safe content; hashes of low-entropy
secrets can leak them through guessing. Secret blocks are excluded without hashing
their contents. Analytics receives numbers and controlled enums, not prompt text.

Redaction must occur before durable context, on every API/export read, and on
structured display fields. Parse usage fields from raw events into an allowlisted
numeric structure before applying text redaction; otherwise a broad regex can
corrupt JSON. Mark retrieved code/log text as untrusted data. A compiled prompt
never grants arbitrary command execution: existing execution authority and bwrap
validation remain the boundaries. The compiler does not sandbox privileged CLIs.

Tests: coverage survives budget pressure; no cross-project/path escape; secret
exclusion; deterministic output/hash; no reviewer self-assessment; required
dependency visible at specified SHA; non-ASCII estimates; oversized required
context fails explicitly; source revision invalidates manifest cache; no raw log
or whole-plan accidental inclusion.

## G. Token and subscription usage instrumentation

Configuration and eligibility snapshot: all four providers are enabled in
`config/orchestrator.yaml`; all four executables resolve on this host. Default DB
records Claude 2.1.267, Codex 0.153.4, AGY 1.1.28 and OpenCode 1.17.13 as AVAILABLE
with no cooldown timestamp. This is a persisted eligibility observation, not a
fresh authentication/quota test. OpenCode is pinned to
`opencode-go/muse-spark-1.3-contributor`/xhigh; the other adapters inherit CLI defaults.
There are no persisted role-priority overrides in this DB.

### What is actually available

| Provider | Observed current evidence | Proposed provenance and limitations |
|---|---|---|
| Claude | Review stdout final `result.usage`, per-model `modelUsage`; input/output, cache creation/read, thinking; model/context metadata. Planner sample has intermediate usage but no final result event | PROVIDER_REPORTED via CLI for final usage. Preserve which result was used. Do not sum duplicate assistant snapshots or add modelUsage to the same run total. Missing terminal usage is PARTIAL/UNKNOWN |
| Codex | Current adapter suppresses usage; sampled stdout has thread ID but no `turn.completed`. Exact thread-matched session has cumulative token_count totals and model | CLI_REPORTED from the session; future stdout turn-final parser must use verified fixtures. Fallback only for GG-recorded thread/cwd/time interval. Use cumulative endpoint or interval delta, never sum cumulative token_count events |
| AGY | Adapter recognizes step_update, assistant and result. Installed help confirms print/stream-json/schema/model options. No AGY run in this default DB, no verified token record | UNKNOWN. Do not infer Gemini SDK counters or quota from the CLI name. Initially report GG prompt/output-text estimates; add a parser only after capturing a legitimate versioned GG fixture |
| OpenCode | `step_finish.part.tokens`: total, input, output, reasoning, cache.read/write. 40 events in implementer sample, 12 in final repair; another repair has none | CLI_REPORTED. Deduplicate step IDs; sum completed-step deltas only. Surface PARTIAL if a started step lacks a finish or terminal contract is absent. Do not interpret the last step as whole-run usage |

For the first OpenCode implementation, summing unique observed step records gives
77,434 input, 2,473,896 cache-read, 11,356 output, 1,574 reasoning and 2,564,260
native total. Their arithmetic matches input + cache + output + reasoning for
these fixtures. That is evidence for this CLI/version's fields, not a portable
definition of total. The first repair has three step starts but only two finishes;
its 38,988 observed native total is a **partial lower-bound observation**, not the
whole repair's exact consumption. Another repair has no token records at all.

For Codex, the matching run spans 02:29:42–02:57:36 UTC and the last usage event
is 02:57:35 UTC. The session's model is `gpt-5.6-sol`. For Claude, review logs report
`claude-sonnet-5`. These are observed models, not assumptions from current defaults.
OpenCode's argv records the configured Muse Spark model/variant. AGY's model is
unknown in this sample. Do not read unrelated CLI configuration to guess it.

### Normalized usage contract

Use nonnegative nullable integers; `null` means unknown, zero requires reported
zero. Record source, completeness, parser version and evidence type independently.
Prefer these fields:

```text
run_id, model_observed, model_requested, provider, role
input_tokens_total             # inclusive of cache only if derivable
output_tokens_total            # inclusive of reasoning only if derivable
cache_read_input_tokens, cache_write_input_tokens
reasoning_output_tokens        # subset of normalized output; never add twice
native_total_tokens            # optional, retain provider's definition
prompt_tokens_estimated, estimate_method
output_text_tokens_estimated   # visible text only, not total model output
source: PROVIDER_REPORTED | CLI_REPORTED | LOCALLY_ESTIMATED | UNKNOWN
completeness: COMPLETE | PARTIAL | UNKNOWN
input_basis / output_basis, parser_version, observation_count
duration_ms, session_ref        # owned session only
```

Keep native field mapping in a small versioned adapter contract. Claude normalized
input includes its uncached plus cache-creation/read fields; its thinking count is
a subset of output. Codex cumulative input includes cached input, and reasoning
is within output. OpenCode fixture output/reasoning are separately additive for
normalization. When semantics cannot be established, leave normalized totals
null and preserve safe native counters; do not invent comparability.

Do not expose a universal `context_tokens` total: cumulative input is not peak
context occupancy. Add `last_turn_input` or `peak_observed_turn_input` only when
events truly provide that measure. Duration comes from monotonic execution time;
historical timestamp subtraction is separately labeled derived wall time.

### Subscription utilization

Count invocations, model turns when observable, success/failure, retries, duration,
queue/provider wait, rate/quota/auth/unavailable events and imposed cooldowns.
Provider run success means the invocation protocol completed, not software passed.
Show both. Track cooldown reason, observation source/time and retry eligibility.

Use separate health and quota dimensions. AVAILABLE/BUSY is execution eligibility;
quota can still be UNKNOWN. RATE_LIMITED/QUOTA_EXHAUSTED is a classified observation,
COOLDOWN is a local policy. Show “eligible to retry in …” rather than “quota resets
in …” unless an actual reset timestamp is supplied and verified. Never display a
fabricated percentage remaining. No monetary cost conversion: Claude costUSD
telemetry is not evidence of a subscription bill.

## H. Analytics redesign

Current `AnalyticsPage.tsx` is 61 lines and polls `/api/analytics` every ten seconds.
`Orchestrator.analytics:448` groups mission statuses, finding severities, run counts,
failure_class=NONE successes, rate/quota failures and average duration. The UI calls
these “estimates” even though counts are database observations. There are no token,
product, role, model, context or acceptance metrics.

Proposed dashboard, with a date range and optional product filter:

1. **Outcome overview:** delivered/blocked products, completed/unverified missions,
   terminal task pass rate, first-pass acceptance and repair rate. Show denominators
   and in-flight work separately. Product duration includes explicit waiting time;
   show active runtime separately.
2. **Provider/model usage:** runs, protocol outcomes, input/output/cache/reasoning
   where defensible, estimated GG prompts separately, duration, retries and quota
   events. Each cell shows source/completeness; aggregation shows measured coverage,
   for example “7 of 9 runs have complete usage.” Never silently treat missing as zero.
3. **Context efficiency:** prompt-size distributions by role, selected/candidate
   context ratio, repeated-block ratio, repair/review share, internal model turns,
   and output/cumulative-input amplification. Drill through to manifests.
4. **Acceptance and interventions:** failing criteria, defect vs environment vs
   external setup, human wait, independent review coverage, repair outcomes.
5. **Product consumption:** provider/model/run/role breakdown for that product,
   with partial and estimated contributions separated. Include phase retries and
   product planning even before a repository exists.

Backend endpoints: additive `/api/analytics/usage`, `/api/analytics/outcomes`,
`/api/analytics/context`, filtered paginated `/api/runs`; preserve `/api/analytics`
until the old UI migrates. SQL over indexed local tables is enough. A daily
materialized rollup is unnecessary until measurement shows query latency matters.

Useful metric definitions:

| Metric | Defensible definition |
|---|---|
| First-pass task success | Accepted task with no repair, eligible denominator excluding cancelled/external-blocked work |
| Repair rate | Tasks/criteria with a code-repair invocation divided by eligible attempted tasks/criteria |
| Tokens per accepted requirement | Product-attributed usage divided by uniquely accepted required IDs; label as coarse shared-work allocation, not causal cost |
| Prompt compression | 1 - selected estimate/candidate estimate for explicit candidate blocks; not savings in subscription quota |
| Repeated-context ratio | Included block estimates whose safe content hashes recur from a comparable previous invocation / included estimate |
| Model effectiveness | Verified outcomes within role/complexity/project-type cohorts, with sample size and uncertainty; never raw finding count as reviewer quality |
| Findings per review | Unique finding IDs by severity, confirmed/resolved status and reviewed diff scope; avoid counting re-flags as new defects |

Warning thresholds should come from evidence. Exact repeated boilerplate is always
worth deduplicating. Budget overflow or missing required coverage is always an
error. Large-prompt/repair-size warnings use a role's observed distribution and
persistently poor outcomes once enough samples exist; until then show comparisons
without red/yellow judgments. “Repair larger than implementation” is a triage
signal, not proof of waste. Do not hardcode claims such as “72% unrelated.” The
current 71.8% observation measures repeated mission text, not irrelevance.

For model comparisons, show raw n and interval estimates; with tiny samples use
“insufficient evidence,” not a leaderboard. Confirmed findings and independently
verified acceptance are better outcome labels than provider self-report. Tokens
can correlate with hard tasks; an expensive model is not necessarily less efficient.

## I. Provider/model routing improvements

Current sequential selection uses role priority, optional saved profile, eligibility
and provider-name separation. Parallel tasks use preferred providers then priorities
and `provider_score`: priority points, global success rate, recent failures and
cooldown penalties. The capability table is unused; score explanations are returned
but discarded. Global CLI success is a poor coding-quality signal.

Introduce operator-configured provider/model profiles with supported role set,
explicit model/effort selection, optional context capacity with provenance,
concurrency limit, enabled state and capability tags. Tags describe demonstrated
or operator-approved suitability; no hardcoded “Claude best architect” marketing.
All adapters should accept requested model/effort only where local CLI contracts
support them. Preserve observed model separately from requested model.

Routing order: hard constraints (available, lease/cooldown, model supports request,
review independence, sufficient known capacity) → operator preferences → verified
historical cohort evidence → latency/consumption tie-breaks. Persist candidate
rejections and selected reason. Use priors/shrinkage so one successful run cannot
overrule an operator preference. Quota failure is not evidence of bad code quality.

A lightweight task classifier is useful as annotations: TRIVIAL/SMALL/MEDIUM/
COMPLEX/ARCHITECTURAL/CRITICAL_REVIEW. Start deterministic from scope, cross-component
interfaces, data/security changes and verification obligations; operator/planner
may override with a reason. Do not make another model call for every label.
Confidence/unknown is explicit. Labels influence context depth and eligible profiles,
not whether security or acceptance checks apply. Unknown work defaults to a capable
configured profile, not an unjustified cheapest model. Adaptive routing comes after
reliable metrics, not before.

Learning loop: persist malformed-output, scope violation, environment/verification
failure, confirmed findings, repair result and prompt version. Automatically adjust
cooldown eligibility and bounded statistical recommendations. Keep prompt/policy
edits, capability claims, acceptance requirements and autonomy budgets human-reviewed.
No uncontrolled self-modifying prompts, embedding every past run, or vector database
is needed. A versioned recipe/lesson linked to a concrete recurrent failure is enough.

## J. Autonomous repair and acceptance improvements

GG already has bounded **mission** repair and two-attempt **phase** retries. It does
not have targeted **product acceptance** repair. A failed criterion leads to BLOCKED
and the UI prominently offers a waiver. Re-running acceptance can reuse that same
failed command/SHA result. A phase retry can rerun planning/implementation/testing/
review even when only one requirement check needs a correction.

The safest extension starts with stronger evidence identity, then repair:

```text
candidate SHA → independent review → criterion execution
                    │                     │
                    └── defect evidence ──┘
                               ↓
                deterministic failure classification
                 ├─ environment → provision/recheck
                 ├─ external/ambiguous/destructive → specific Human Gate
                 └─ code defect → bounded scoped repair
                                  → new SHA → independent re-review
                                  → failed + affected criteria
                                  → all required checks in fresh checkout
                                  → delivery
```

Persist `acceptance_attempts` and append-only criterion attempts with candidate
SHA, criterion content hash, environment fingerprint, command, outcome and evidence
path. Keep `criterion_results` as a latest-result view/cache. Explicit recheck
creates a new attempt, including on unchanged SHA. Cache only immutable-equivalent
successful checks; failed environment or explicitly requested reruns must execute.
Reject duplicate/empty criterion IDs at plan validation—current validator accepts
duplicates although storage is keyed only by product/criterion.

Before delivery, run all mandatory criteria in the isolated checkout of the exact
candidate SHA, and verify tests did not change tracked candidate code. Separate
read-only validation artifacts from source writes. Record environment/lockfile and
reviewed SHA. Final-validation repairs invalidate the prior review. Product-wide
cross-phase checks can be deterministic; a final LLM review is warranted for
cross-component/high-risk changes, not automatically for every trivial product.

Proposed repair ticket: product/phase-attempt ID, criterion/finding IDs, failure
evidence, base SHA, scope, diagnosis class/confidence, attempt count, previous repair
IDs, status, resulting SHA, reviewer and recheck IDs. Unique active ticket per
criterion/finding + base SHA prevents duplicate scheduling. Use the existing mission
executor with a repair-focused execution specification, not a second orchestration
subsystem. Its initial role is REPAIR; do not pay to re-plan the whole product.

Default budget proposal: reuse the existing configured repair ceiling initially,
but give product acceptance its own persisted counter and aggregate project ceiling.
Permit at most one active repair wave per repo. Stop earlier for unchanged code,
unchanged failing signature after an attempted relevant fix, contradictory checks,
missing authority, or exhausted budget. Exact ceiling values are operator policy,
not claims about model capability. Never reset counters on restart or automatic
phase retry. Manual budget extension must be visible.

Human work: credentials/account setup, ambiguous product/legal decisions,
destructive operations, unavailable external services, contradictory requirements,
or exhausted repairs. A missing cached package manager is an environment problem,
not a reason to ask a coding model to edit application logic. Current environment
markers are narrow and only applied inside final validation; propagate diagnosis
to outer phase retries and product acceptance.

Tests: failed R4 creates one targeted repair, not a full-roadmap rerun; restart
does not duplicate tickets; cancel holds writer ownership until exit; repaired SHA
is independently reviewed; recheck executes on unchanged SHA when requested;
external prerequisite opens one actionable gate; unchanged failure terminates;
waivers never auto-create; full fresh-checkout criterion replay is required.

## K. Backend improvement audit

Effort: S = one focused change (roughly 1–2 engineering days), M = several related
changes (3–5 days), L = a coordinated schema/engine/API increment (about 1–2 weeks).
These are planning ranges, not commitments. All findings below refer to current
source unless explicitly marked historical. Proposed tests are not claimed run.

### B01 — P1: unify invocation ownership, outcomes and persistence

**Problem/evidence:** `project_engine.py:382` directly executes a provider in `/tmp`,
without a run row, spawn identity callback, tracked cancellation or result.ok check.
A deterministic fake QUOTA_EXHAUSTED result produced three “successful” provider
runs, zero run records, and a BLOCKED plan. `parallel_engine.py:977` also omits
on_spawn on DAG planning. Other paths persist different run metadata.
**Impact:** misleading availability/analytics, unowned work after restart, lost
planning context and duplicate capacity consumption.
**Change:** shared invocation service, durable row/lease before spawn, one outcome
normalizer, common cancellation/recovery. Isolated per-run planner directory.
**Files:** base/process/registry, all engine call sites, schema/API.
**Risk/effort:** medium-high/L; avoid rewriting phase policy at the same time.
**Acceptance test:** failed planner is one failed run, cooldown applied, no false
success; SIGKILL during any of five invocation paths leaves a recoverable identity.

### B02 — P1: stop accepting intermediate success as final success

**Evidence:** `providers/classify.py:121` calls the adapter success hook before
checking exit code. `codex.py:78` accepts `item.completed`; OpenCode accepts
`step_finish`. A pure-function probe with item.completed then failing turn/nonzero
exit returns NONE. This is a demonstrated classification defect, not a quota event
from a live provider.
**Change:** parse ordered terminal outcome events; explicit terminal error/nonzero
exit takes precedence over earlier progress. Track parse completeness separately.
**Files:** classify/base/adapter parsers and captured fixtures.
**Impact:** truthful success and failover/routing metrics. **Risk/effort:** medium/S–M.
**Test:** completed tool item then quota/error exits failure; benign limit telemetry
followed by genuine successful terminal event remains success.

### B03 — P1: make acceptance evidence correspond to delivered code

**Evidence:** `project_engine.py:1405` chooses SHA before working-tree verification
and criteria; checks may modify files; it does not recheck tracked cleanliness
after those commands. `_fresh_checkout_verify:1494` reruns toolchain, not specific
criteria. `run_acceptance:1654` permits BLOCKED/WAITING_FOR_HUMAN without first
establishing completed phases or exclusive verification ownership.
**Change:** durable acceptance operation, quiescent candidate SHA, all required
criteria in fresh checkout, post-check source integrity and reviewed-SHA binding.
**Impact:** prevent unsupported delivery claims. **Files:** project/criterion/verify,
schema. **Risk/effort:** high/L. **Test:** a check that fixes code while passing cannot
certify the old commit; acceptance during active phase is rejected/queued.

### B04 — P1: correct criterion identity and explicit rerun semantics

**Evidence:** `product_plan.py:131` validates requirement IDs but not unique global
criterion IDs; a duplicate-criterion probe returns no errors. `criterion_results`
has PK `(project_id, criterion_id)`. `project_engine.py:1314` reuses failed checks
on identical command/SHA; description/environment changes are not cache identity.
**Change:** reject duplicate/blank criterion IDs, content/environment versioning,
append-only attempts, explicit force-recheck semantics without deleting evidence.
**Files:** product_plan/project_engine, migrations, API/UI. **Risk/effort:** medium/M.
**Test:** repeated ID rejected; fixed environment on same SHA can be explicitly
rechecked and both outcomes remain inspectable.

### B05 — P1: preserve dependency artifacts in DAG execution

**Evidence:** `parallel_engine.py:1255` creates task worktree without dependency
base; `task_worktree.py:65` defaults to main repo HEAD. Final integration runs only
after all tasks terminate (`parallel_engine.py:461`). Prompts have no dependency
results (`1485`).
**Change:** deterministic dependency snapshot: branch from mission base and merge
completed ancestor commits in topological order before consumer launch, recording
the resulting base and dependency IDs. Independent tasks remain isolated. Resolve
conflicts before launching a consumer. Do not simply merge every task into main
early without preserving review/integration boundaries.
**Impact:** dependent tasks can build/test upstream code. **Risk/effort:** high/M–L.
**Test:** A exports an API; B cannot start until A completes and imports A's exact
artifact; two-parent conflicts block before CLI launch; restart reproduces base.

### B06 — P1: cancel real task execution before releasing ownership

**Evidence:** task cancel endpoint `api/app.py:656` updates CANCELLED and releases
locks/reservations but does not interrupt the corresponding adapter/runner.
`parallel_engine.py:1372` can later write COMPLETED/FAILED over that state.
Parallel task cancellation paths also need process lifetime tests, not just row
assertions. Retry endpoint resets attempts without a full task-state guard.
**Change:** route cancellation through engine/run ownership; CANCEL_REQUESTED →
interrupt/reap → terminal → release. Validate retryable states and preserve attempt
history. **Impact:** honest control and no concurrent old/new writer. **Risk/effort:**
high/M. **Test:** cancel a slow real local fake subprocess, verify it exited before
capacity is reused and no later completion overwrites cancellation.

### B07 — P1: make product planning/start/pause real durable operations

**Evidence:** `generate_plan:333` has no operation lock/revision compare-and-swap;
it accepts any nonterminal state. Regeneration doesn't synchronize existing phase
rows. `revise_plan:421` can update running phase specs and retains removed pending
rows. `pause_project:1072` pauses current mission(s), but persists no project pause
intent and has no product resume endpoint. PLAN_READY auto-advance is unreachable
from normal active-product selection; approval field is unused.
**Change:** revision-checked plan operations with operation ID; prohibit unsafe
in-flight rewrites; persisted paused flag and pause/resume actions; explicit approved
revision and auto-start semantics; preserve superseded phase attempts.
**Impact:** predictable operator actions and restarts. **Risk/effort:** medium-high/M–L.
**Test:** two plan requests create one active operation; cancel cannot leave a new
plan revision after terminal state; pause survives restart and no new phase launches;
explicit approval starts exactly one approved revision.

### B08 — P1: preserve attempts and all blocking findings

**Evidence:** phase retries clear/replace mission_id (`project_engine.py:915`);
`_blocking_findings_locked:1372` visits only current phase mission links. Delivery
also traverses latest links. This loses direct product attribution for earlier
attempt costs/findings. Mission repair considers open HIGH/BLOCKER only; product
acceptance also blocks selected MEDIUM findings. MEDIUM can therefore stall a
product without entering automated mission repair.
**Change:** `phase_attempts` lineage plus product-linked repair targets; query all
relevant unresolved findings with explicit supersession/resolution. Align repair
eligibility with product acceptance policy. **Risk/effort:** medium/M.
**Test:** retrying a phase retains old cost/evidence and cannot erase an unresolved
security finding; a repairable MEDIUM requirement issue gets a scoped repair.

### B09 — P1: tighten review validity and provenance

**Evidence:** `review.py:104` accepts an array if at least one entry is valid,
silently dropping malformed entries. `persist_findings:214` permits empty resolution
evidence. Engines identify only the last IMPLEMENTATION provider; TESTING/REPAIR
writers are ignored. Final-validation repair skips rereview (`engine.py:950`).
**Change:** all entries validate or the review is incomplete; nonempty verifiable
resolution evidence, run/SHA references, all-writer provenance; re-review new repair
SHA. **Impact:** prevent overstated independent review. **Risk/effort:** medium/M.
**Test:** mixed valid/malformed findings cannot pass; empty evidence cannot resolve;
repair writer chosen as reviewer is disclosed/deferred; repaired SHA re-reviewed.

### B10 — P1: supply scoped intent and evidence instead of rediscovery

**Evidence:** prompt map in D; absent architecture/requirements in product phase,
200-char planner handoff, generic DAG prompt, three copies of review instructions.
**Change:** role policies/context compiler in F; no extra LLM invocation by default.
**Impact:** quality and token efficiency. **Files:** prompt builders/shared compiler,
schema/API inspector. **Risk/effort:** medium/M–L. **Test:** mapped requirements and
architecture sentinel survive into implementer/reviewer requests; unrelated plan
phases and implementer self-assessment do not; mandatory overflow is explicit.

### B11 — P1: validate the execution environment before expensive work

**Evidence:** `workspace.py:47` detects root manifests heuristically; Python implies
pytest/ruff/mypy even if not installed. pnpm is detected for verification but fresh
install chooses npm for package.json, and product planner's command contract insists
on npm. Nested monorepo toolchains aren't discovered. Current DNS fix prevents some
inner repairs, but outer phase retry still repeats the entire mission.
**Change:** declared `ExecutionEnvironment` recipe (manager/version/lockfile, working
directory, install/check commands, sandbox requirements); preflight with cache and
namespace checks. Propagate environment classification; don't rerun code generation
for known missing tooling. **Risk/effort:** medium/M. **Test:** pnpm workspace and
Python backend+frontend recipe reproduce offline checks after isolated install.

### B12 — P2: shorten lock scopes and make state mutations atomic

**Evidence:** shared `_advance_lock` is held while acceptance runs subprocesses;
`_scheduler_loop` awaits `advance_all` before processing workspace waiters. Gate
validation already demonstrates release/revalidate behavior (`1129`). `db.cursor`
commits per call; nested events inside transactions can commit earlier work through
the same connection. Plan/phase/DAG mutations span multiple commits; migrations use
executescript with separate version recording.
**Change:** claim short durable operations, run slow work unlocked, compare state
version before applying results. Transaction helpers with explicit event-after-commit;
atomic migrate/version bookkeeping. **Impact:** other products remain responsive;
crash recovery sees coherent records. **Risk/effort:** medium/M. **Test:** acceptance
for A doesn't delay cancel/start for B; injected crash leaves no partial DAG/revision.

### B13 — P2: make capacity and lock namespaces consistent

**Evidence:** reservations cover DAG tasks only. `_arbitrate_provider` admits BUSY
with spare capacity, but `try_reserve_provider:146` rejects BUSY. Registry success
sets AVAILABLE irrespective of other active executions. Scope keys are plain paths
without repository identity (`task_locks.py`, `readiness.py:108`), so unrelated repos
can contend. Configuration advertises per-project overrides that are not loaded.
**Change:** run-scoped leases for every invocation, availability derived from leases,
canonical workspace namespace for locks, one configuration resolver with validated
effective settings. **Risk/effort:** medium/M. **Test:** limit=2 permits two runs,
third waits; one completion doesn't free both; same path in separate repos doesn't
conflict. Fold lease work into B01, namespace cleanup can follow separately.

### B14 — P1/P2: secure bootstrap and isolate evidence attribution

**Evidence:** adopted non-Git directories are initialized with direct `git add -A`
in `ensure_target_repo:555` before checkpoint filtering; this can stage existing
secrets. `_build_delivery_report:1587` collects global last-50 test events. Verification
from product acceptance publishes an empty mission ID and no product ID. Raw logs
are intentionally sensitive. `api/projects/.../git` serves diff without explicit
redaction; human inputs also lack uniform persistence redaction.
**Change:** bootstrap through existing safe staging, verify target boundaries before
directory creation, scope all evidence to product/attempt/SHA, serve redacted diff
and safe diagnostic fields. **Risk/effort:** medium/S–M (bootstrap P1, attribution P1).
**Test:** non-Git folder with `.env` never stages it; product A delivery contains no
product B command; protected raw values never reach context/analytics/export.

### B15 — P2: robust stream parsing and replay

**Evidence:** `process.py:113` uses StreamReader.readline with default line limit;
provider JSON events can be large (observed max 59,701 chars in one log).
Pump exceptions are collected with `return_exceptions=True` and ignored. Tails
are bounded by line count, not bytes; final pumps can wait on inherited open pipes.
`EventBus.history:93` returns oldest N events; subscriber queues drop silently.
Transient buffer count is bounded per mission, not across all retained missions.
**Change:** bounded chunk-based NDJSON parser with explicit oversized-event policy,
drain/error visibility and bounded reap; numeric usage accumulator outside tails;
cursor replay/snapshot recovery, terminal-buffer eviction.
**Risk/effort:** medium/M. **Test:** >64KB event, truncated JSON, duplicate/reordered
usage and surviving pipe descendants do not hang or falsely complete a run; reconnect
after >500 durable events obtains the latest state.

### B16 — P2: preserve maintainability without a rewrite

The 1,688-line product coordinator, 1,580-line parallel engine, 967-line sequential
engine and 905-line API share responsibilities inconsistently. Extract invocation,
context, acceptance and operator-action services at their existing seams; keep the
state machines. Standard-library formatted logging is used today, not structured
logging. Add run/product/task IDs and safe JSON events for diagnostics. No GitHub
Actions workflow exists in this checkout; add deterministic CI after stabilizing
the relevant contracts. Keep fake providers but add behavioral fixtures: a toolchain
script that simply exits zero proves orchestration, not product requirement quality.
**Risk/effort:** low-medium/M across focused changes. **Test:** contract suites run
against both mission engines; no real CLI/auth required.

### Failure intelligence contract

Keep transport/provider failure separate from engineering outcome. Proposed
`FailureDiagnostic` contains stage, cause, evidence references, confidence,
retryability, scope and recommended action. Reuse current FailureClass for CLI
AUTH/RATE_LIMIT/QUOTA_EXHAUSTED/OVERLOADED/TIMEOUT/CRASH/CANCELLED. Add the following
engineering causes only where they change routing or operator action:

| Cause | Evidence / next behavior |
|---|---|
| FORMAT | Schema/marker violation; bounded contract correction, separate from bad implementation |
| CONTEXT_MISSING / CONTEXT_OVERFLOW | Required block absent or measured budget rejection; compiler repair/task split, not provider blame |
| IMPLEMENTATION_DEFECT | Failed reproducible behavior in valid environment; scoped code repair |
| ENVIRONMENT | Missing tool/cache/sandbox or known provisioning failure; preflight/provision/recheck |
| DEPENDENCY | Missing artifact/interface/failed predecessor; resolve dependency or block consumer |
| EXTERNAL_OR_HUMAN | Credential/service/decision/authority prerequisite; explicit gate with required action |
| ORCHESTRATOR | Ownership, persistence, spawn-handshake or state-transition fault; preserve evidence and stop/repair orchestrator |
| UNKNOWN | Evidence insufficient; show unknown, bounded diagnostic step |

Review and verification are stages, not competing causes: a verification failure
can be environment or implementation. “Prompt failure” is too broad to blame a
template from one failed run; record suspected contributing prompt/policy version
and missing-context evidence instead. A model should not assign itself innocence
through an unsupported CONTEXT_MISSING label. Structured evidence drives attribution.

## L. Frontend and UX audit

This is a source-and-component-test audit, not a new live browser dogfood. All pages,
shared API/hooks/WS behavior and operator components were inspected. Existing
frontend tests pass; they do not cover the lifecycle page's action contract well.

The UI already has useful controls: Generate Plan, Start, Advance Now, Pause,
Cancel, Run Acceptance, Retry Phase, mission retry/resume, task retry/cancel, Human
Gate resolution, manual DAG editing, priority editing, provider detection/toggle,
Git ledger, diff display, conflict instructions, bounded task logs, terminal copy,
and explicit waivers. Do not count these as missing features.

Main problems: duplicate meanings of “project”; default product detail opens the
plan even during execution; status labels expose engine internals without next
actions; execution links point to `/` rather than selecting the phase mission;
large all-data polling; errors often ignored by callers of `usePolling`; and
acceptance failures offer waiver before diagnose/recheck/repair. Mission Control
renders many panels at once. Parallel current_provider/live stream isn't reliable
enough to drive the requested model activity panel yet.

### Proposed project detail

```text
Product name   [Running]                    Pause   Actions…
Now: Frontend filters · Implementer · OpenCode / configured model
Why: Phase 3 / R4    Next: Independent review    Needs you: none
Capacity: 8 runs · prompt estimate ~… · reported usage 6/8 complete

Plan ✓ → Foundation ✓ → Backend ✓ → Frontend RUNNING → Acceptance → Delivery

[Overview] [Plan] [Activity] [Evidence] [Files & Delivery]
Current work / most relevant failure / one recommended next action
Expanded on demand: task → invocation → context, log, diff, checks
```

Avoid progress percentages inferred from phase counts. Show completed phases/tasks
and current activity; unknown remaining effort is honest. A queued provider card
shows the actual wait reason and eligible-to-retry timestamp. Elapsed runtime is
measured; live output tokens appear only if observed, otherwise label visible-text
estimate or unknown. Never present the initial prompt estimate as live context
window occupancy.

### Actions with concrete contracts

Paths below use `/api/product-projects/{id}` as product prefix. New mutating actions
should accept expected state version + idempotency key, return operation ID, and
expose busy/retryable failure state. Client disables duplicate submissions, but the
server enforces legality. A 409 presents current state and refresh, not silent loss.

| Action / page | Current state | Proposed backend/interaction | Acceptance test |
|---|---|---|---|
| Generate / Regenerate Plan | Exists, long synchronous request | POST `/plan` returns 202 operation; forbid conflicting active operation; show progress/cancel and revision diff | Double click creates one planner run; failure visibly leaves recoverable state |
| Approve & Start | Start exists; approval field ineffective | POST `/approve` with revision, then start approved revision atomically; label existing button accordingly when implemented | New revision invalidates old approval |
| Pause / Resume Product | Pause has no durable product state; resume missing | POST `/pause`, `/resume`; persisted pause intent checked by coordinator | No new phase after pause/restart; resume retains completed work |
| Retry Phase | Exists | Preserve phase-attempt lineage and expected state; show diagnosis and replay scope before triggering | Only terminal unsuccessful attempt retries; old evidence retained |
| Explain Failure | Missing | GET `/failures/{evidence_id}` returns deterministic classification, command, expected/observed, next actions; optional LLM explanation separately invoked | No provider called merely to open explanation |
| Re-run Criterion | Missing | POST `/criteria/{criterion_id}/checks` creates explicit new check attempt on chosen candidate SHA | Same-SHA failed result is re-executed; history retained |
| Re-run Verification / Fresh Checkout | Only combined acceptance exists | POST `/verification-runs` or `/acceptance-attempts` with mode enum and candidate SHA, using shared ownership | Reject run while writer active; show progress and exact evidence |
| Repair Finding / Blocking Findings | Missing | POST `/repairs` with validated finding/criterion IDs; one active ticket per target; review/recheck chain | No waiver, whole-plan regeneration or duplicate repair hidden behind click |
| Add Repair Phase | JSON revision currently possible | Prefer repair ticket action above; advanced plan editor can create explicit typed phase via revision API | Does not alter unrelated completed phase definitions |
| Validate Environment | Repository Validate only inspects metadata | POST `/environment-checks`; local preflight and explicit isolated provisioning operation | Distinguishes missing bwrap/cache/tool version from app test failure |
| View Context | Missing | GET `/api/runs/{run_id}/context`; block decisions, coverage, estimate/version/source | Missing R4 is evident; secrets/path escapes cannot display |
| View Diff / Commit / Worktree | Git info exists, unscoped working diff | GET run artifacts/diff bounded by recorded base/head and project; client Copy SHA/path | Correct phase selected, no arbitrary host path endpoint |
| Resolve Human Gate | Exists | Retain schema; surface validation errors locally and pending validation operation | Failed validation leaves gate open and visible |
| Export Delivery / Copy Run Command | Report exists, generic instructions | GET scoped delivery export; copy command from validated execution recipe | Export states checks/waivers/SHA; no fictional runnable command |
| Open Generated App | Missing | Only when recipe records verified local URL and app process ownership; separate start/stop app operation if needed | External/untrusted URLs not auto-opened; correct managed process tracked |
| Architecture Only / Explain Decision | Missing | First show stored decision rationale; later explicit revision proposal touching architecture subset with impact preview | No hidden LLM call or silent architecture mutation |

More buttons alone are not the solution. Show one primary action for current state,
secondary context actions in the relevant panel, and advanced controls in an action
menu. Waive selected criteria requires per-target selection and a recorded reason;
do not promote blanket waiver as the default recovery path.

A Context Inspector is high-value once manifests exist. Show required IDs, included
files/commits/dependency summaries, findings, omitted blocks/reasons, estimate,
template/policy/model and immutable source versions. Default to this explanation,
not a giant raw-prompt textarea. “Not captured for this historical run” is a valid
state. Numeric analytics and prompt preview require separate access/export paths.

Polling need not be replaced wholesale. Add abort/version guarding to `usePolling`
(TaskLogPanel already has stronger request lifecycle handling), render errors,
pause inactive tabs, stop fetching all missions for one product, and use a
product-scoped status snapshot. Then use existing global events to invalidate
queries, with periodic polling as reconciliation. Cursor replay is a correctness
improvement, not a reason to add a distributed event bus.

## M. Documentation changes

README now describes both workflows, current model config, real startup/token/state
paths, acceptance and sandbox limits. Root ARCHITECTURE is a concise current-code
map; this document is the separate proposed blueprint. DEVELOPMENT removes the
invalid uvicorn-entrypoint command and obsolete test count. PROVIDERS describes
actual model flags, usage availability and the classifier limitation.
TROUBLESHOOTING removes broad provider-kill advice, fictional quota reset claims,
incorrect cooldown-toggle advice and the root-relative DB assumption. SECURITY
clarifies internal log exclusion and the limits of bootstrap/run persistence.
Parallel/DAG docs are synchronized where they claimed unimplemented behavior;
historical release reports remain historical.

AGENTS.md and CLAUDE.md did not exist in the repository. AGENTS is a concise
canonical contributor guide, CLAUDE a short pointer. Therefore injected context
**increases from zero repo-local instruction text**, rather than claiming a reduction
from nonexistent files. The large audit is not automatically imported. Approximate
sizes below use characters/4, never exact tokenizer precision:

| File | Before characters / words / estimated tokens | After characters / words / estimated tokens |
|---|---:|---:|
| README.md | 3,753 / 525 / ~939 | 4,441 / 551 / ~1,111 |
| AGENTS.md | absent | 2,236 / 279 / ~559 |
| CLAUDE.md | absent | 573 / 66 / ~144 |

Potential combined repository instruction overhead increases by ~703 estimated
tokens if both files are loaded. Actual automatic loading depends on the CLI;
CLAUDE contains pointers, not an automatic import of this audit. The final
documentation commit is recorded in the handoff.

## N. Important additional opportunities

1. **Environment recipes and starter templates.** After twenty projects, repeat
   setup failures and missing run instructions will be more frustrating than model
   selection. Start with two proven local recipes (Node/npm and Python/uv), then
   add pnpm/monorepo support honestly. Templates include locked dependencies,
   tests, acceptance harness, run command and sandbox preflight. Templates are
   versioned starting points, not a forced product architecture.
2. **Evidence retention and storage inventory.** Raw logs, clones and worktrees
   grow independently of bounded event buffers. Expose per-product storage sizes,
   active references, retention settings and a dry-run cleanup preview. Preserve
   failed worktrees and evidence needed for unresolved findings. `remove_task_worktree`
   currently trusts a `gg/` prefix and uses force removal; strengthen ownership,
   merge status, resolved path and process checks before exposing cleanup. Do not
   run cleanup during this audit.
3. **Reproducible application launch.** Deliver source SHA plus environment recipe,
   dependency lock digest, exact checks and tested launch instructions. A generic
   “install dependencies” report is insufficient for daily product handoff.
4. **Task/evidence search and bookmarks.** Add indexed local search for project,
   task, criterion, finding, commit and invocation; avoid scanning raw logs by
   default. Deep links should survive reload and select the correct mission.
5. **One backend owner and consistent state root.** Add a local lockfile/owner
   handshake and explicit state directory shared by CLI/Vite/Tauri. A desktop
   health response from some other backend must not certify this child started.
   Package migrations/config as resources before promising installable desktop use:
   current pyproject wheel packages only `src/orchestrator`, while loaders resolve
   migrations/YAML relative to the source checkout.
6. **Acceptance quality lint.** Structural validity is not behavioral specificity.
   Warn when many distinct requirements use the same broad `npm test`, criteria
   only install dependencies, or a phase claims requirements without a behavior
   probe. Require observed expected/failing behavior for critical criteria. Do not
   infer correctness from an exit-zero test wrapper.
7. **Terminal-state and attempt hygiene.** One attempt ledger simplifies debug,
   retry economics, finding provenance, context retrieval and delivery together.
   Normalize lowercase sequential task states and uppercase DAG states at API
   boundaries; don't let metrics accidentally omit an entire execution mode.
8. **Local privacy is not offline execution.** GG analytics stay local, but
   provider CLIs send selected repository context to their services. README/docs
   should not let “local-first” imply code never leaves the machine.

## O. Prioritized improvement matrix

Impact: H/M/L; token impact means expected efficiency opportunity, not a measured
savings promise. Reliability is separated because it often matters before adding
autonomy. Dependencies refer to the IDs in K.

| ID | Priority | Improvement / problem solved | Quality | Tokens | Autonomy | Reliability | UX | Effort | Risk | Dependencies |
|---|---|---|---|---|---|---|---|---|---|---|
| B01/B02 | P1 | Uniform durable invocation + truthful terminal outcomes | H | H | H | H | M | L | M–H | none |
| B14 | P1 | Safe bootstrap + product-scoped evidence | H | L | H | H | M | S–M | M | none |
| B10 | P1 | Role-specific compiler, missing architecture/requirements, prompt manifests | H | H | H | M | H | M–L | M | B01 |
| B03/B04 | P1 | Exact-SHA acceptance, criterion identity/history/recheck | H | M | H | H | H | L | H | B01, B14 |
| B08/B09 | P1 | Attempt lineage, writer provenance, complete review contracts | H | H | H | H | H | M | M | B01 |
| B05 | P1 | Dependency code visible before consumer execution | H | H | H | H | M | M–L | H | B01 |
| B06 | P1 | Cancel/retry process ownership | H | M | H | H | H | M | H | B01 |
| B07 | P1 | Durable plan/start/pause/resume contract | M | M | H | H | H | M–L | M–H | B01 |
| B11 | P1 | Environment recipe/preflight, avoid futile code repairs | H | H | H | H | H | M | M | B01 |
| J | P1 | Targeted bounded acceptance repair | H | H | H | M | H | M–L | H | B03/04/08/09/10/11 |
| H/L | P1 | Honest usage analytics and actionable product overview | M | H | M | M | H | M | L–M | B01/B07/B10 |
| B12/B13 | P2 | Short transactions/locks, namespaced capacity | M | M | M | H | M | M | M | B01 |
| B15 | P2 | Robust streams and cursor replay | M | M | M | H | H | M | M | B01 |
| I | P2 | Evidence-based model profiles and complexity hints | H | H | M | M | M | M | M | B01/B09/H |
| N | P2 | Recipes, delivery commands, storage/search, packaging | M | M | H | H | H | M | M | acceptance/invocations |
| F reuse | P3 | Same-task session reuse, optional handoff refinement | uncertain | potentially H | M | uncertain | L | M | H | manifests/usage/cohort evidence |

Quick wins: remove duplicate role instructions; repair competing output contracts;
reject duplicate criterion IDs; fix final success precedence with fixtures;
redact/scoped delivery queries; deep-link mission selection; show polling errors;
correct startup/docs. Each merits a focused implementation PR after this design
pass; only documentation is changed here.

Foundational work: invocation ownership/usage, context compiler, attempt/evidence
identity, environment recipes and operator state transitions. Experimental: session
reuse, optional LLM handoff compression, adaptive routing and architectural-only
regeneration. **Do not build:** mandatory prompt-rewriter model, always-on LLM
verifier/coordinator, universal huge memory prompt, provider quota percentages,
fictional API billing, automatic policy/requirement rewrites, vector-RAG memory,
distributed services/queues/Kubernetes, or a button for every internal state.

## P. Recommended implementation sequence

The order is bounded and dependency-driven. Stop each increment at its acceptance
criteria and dogfood it before expanding. Quick UI deep links/error rendering can
be an independent small PR, but do not expose a mutating button before its contract.

| Increment | Objective/components | Database/API/frontend | Tests and migration risk | Dogfood and acceptance | Complexity |
|---|---|---|---|---|---|
| 1 | Auditable invocation boundary, truthful failures/usage (B01/B02); narrow bootstrap secret fix independently | Add run owner/model/status/context/usage metadata and run leases; common execution service; run detail API and small read-only inspector | Five execution call paths, failover/cancel/restart, numeric parsers; nullable additive columns preserve old runs; no automatic history import | Existing small mission plus product plan, one artificial provider failure; every call has one run, no false success, usage unknown/partial explicit | L, roughly 5–8 focused days |
| 2 | Role-specific compiler and environment facts (B10/B11); remove redundant phase planning when task specification is already complete | Context block manifests, policy/template versions, environment recipe records; context inspector | Required coverage, reviewer isolation, invalidated references, tokenizer fallback; legacy prompts retain compatibility policy | New simple product with stack constraint and two requirements; every agent sees relevant architecture/criteria and no unrelated roadmap; preflight catches unavailable tool without coding call | M–L, 4–7 days |
| 3 | Evidence and ownership integrity before more automation (B03/04/06/08/09) | Phase/acceptance/criterion attempt ledger; exact candidate SHA, writer provenance; recheck/cancel contracts and evidence UI | Duplicate IDs, same-SHA recheck, dirty checks, multiple writers, retry finding retention; old results marked legacy/unbound rather than silently certified | Existing passing product with one intentionally failing requirement; cannot deliver old SHA or lose failed findings; manual recheck succeeds after environment change | L, 6–10 days |
| 4 | Targeted autonomous acceptance repair (J) | Repair tickets/counters; repair-first execution spec, independent rereview, criterion replay; Repair and failure-explanation actions | Budget exhaustion/restart/cancel/dedup/external gate; explicit defaults for old projects | Broken R4 repaired without regenerating plan or waiving; irreparable external credential request opens one precise gate; no-progress stops | M–L, 4–7 days |
| 5 | Dependable parallel execution and lifecycle controls (B05/B07/B12/B13) | Dependency snapshot SHAs, workspace-scoped locks, operation versions, approved revision and pause/resume | A→B dependency artifact, shared ancestors/conflicts, cancellation, concurrent plan requests, restart pause; existing DAGs retain original artifacts until explicitly retried | Backend exports API, frontend consumes it, review validates combined product; pause prevents new phase launch; no shared DB/process owners | L, 5–9 days |
| 6 | Operator dashboard, useful analytics, profile routing recommendations (H/I/L/N) | Filtered aggregations/run search, availability evidence, validated launch/delivery recipe; activity/evidence tabs and contextual actions | Unknown/partial aggregation, cohort denominators, stale polling, deep links, export privacy; old data excluded from unsupported comparisons | Operator answers what/why/next/needs-me/verified/capacity from one product page; routing reasons visible; no quota percentage or invented spend | M–L, 4–7 days |

If the next dogfood primarily uses dependent DAGs, move Increment 5's B05 snapshot
fix ahead of Increment 2; it is a prerequisite for meaningful DAG quality testing.
For the intended browser Idea→Product path, sequential execution is current reality,
so context/evidence/repair produce the earlier benefit. These are estimates for
focused implementation work, not a six-month roadmap or a requirement to finish
every dashboard feature before using GG again.

## Q. Implementation-ready specification: auditable invocation boundary

### Objective and scope

Every GG-controlled provider invocation must have one durable identity, owner,
capacity claim, prompt measurement, authoritative outcome and honest usage record.
Product planning must participate in recovery/cancellation/accounting. Preserve
existing prompts initially so observed differences reflect instrumentation/outcome
fixes, not simultaneous prompt redesign. This is the highest-leverage first
increment because both the compiler and useful analytics depend on reliable runs.

This increment includes final-outcome classification fixes and a minimal run/context
inspector. It does **not** implement full role-based retrieval, adaptive routing,
acceptance repair, global session scraping, a new agent framework or an LLM prompt
rewriter. Bootstrap secret-safe staging is a small separate prerequisite fix for
adopting non-Git directories; it can be reviewed independently.

### Files to modify and new files

Modify `models.py` for typed operation/run/usage enums; `providers/base.py` for
request/result metadata and observer interface; four real adapters for typed
event parsing; `providers/classify.py` for terminal precedence; `process.py` for
bounded raw-event observation and cancellation/drain behavior; `orphans.py` and
`orchestrator.py` for owner-aware recovery; `engine.py`, `parallel_engine.py`,
`project_engine.py` for service calls; `reservations.py`/registry for unified
capacity; `api/app.py` for read APIs and managed planning actions. Update effective
configuration only with explicit defaults and source validation.

New backend files (proposed names):

- `invocations.py`: durable call lifecycle; no mission phase policy.
- `usage.py`: normalized observation types, reducer and estimator interface.
- `context_manifest.py`: measured legacy prompt manifest, ready for compiler blocks.
- `operations.py`: small durable long-operation owner for product planning;
  acceptance may reuse it later, not a generic workflow engine.
- `migrations/0010_invocation_observability.sql`: additive schema.
- Tests: `test_invocations.py`, `test_usage.py`, `test_context_manifest.py`,
  `test_planning_operations.py`, `tests/fixtures/provider_events/` sanitized fixtures.

Frontend: modify `lib/types.ts`, `lib/api.ts`, MissionControlPage and
LifecycleDetailPage; add `RunInspector.tsx` and `UsageValue.tsx` with tests. Keep
Analytics' existing contract, optionally add a measured-coverage summary from the
new endpoint. A full dashboard is later.

### Schema

Extend `provider_runs` with nullable ownership/identity fields:

```text
product_project_id REFERENCES product_projects(id)
phase_id REFERENCES project_phases(id)
operation_id REFERENCES orchestration_operations(id)
attempt_number, retry_of_run_id
stage                           # product_plan, mission_plan, dag_plan, task, review, repair
run_status                      # PREPARED, WAITING, STARTING, RUNNING, SUCCEEDED,
                                # FAILED, CANCELLED, INTERRUPTED, UNKNOWN
model_requested, model_observed, effort_requested, cli_version, session_ref
duration_ms
prompt_template_version, context_policy_version
```

Existing mission_id/task_id remain optional. Distinguish repository `project_id`
through mission join from explicit product_project_id; do not overload either ID.
No normal-mode scan of arbitrary legacy rows to infer missing ownership.

New `run_context_manifests`:

```text
run_id PRIMARY KEY REFERENCES provider_runs(id)
schema_version, prompt_hash, hash_basis='redacted_rendered_utf8'
prompt_chars, prompt_bytes, prompt_words
estimated_prompt_tokens, estimator_id
blocks_json, capture_status, redaction_status, created_at
```

`blocks_json` initially records a `legacy_prompt` block, plus separately accessible
builder sections where already explicit. Do not claim missing requirements were
intentionally omitted until the context compiler supplies selection decisions.
Record exact prompt length in memory before redaction; hash only safe persisted
representation, with basis explicit. Do not retain raw original prompt by default.

New `run_usage` (one normalized aggregate per run initially):

```text
run_id PRIMARY KEY REFERENCES provider_runs(id)
input_tokens_total NULL, output_tokens_total NULL
cache_read_input_tokens NULL, cache_write_input_tokens NULL
reasoning_output_tokens NULL, native_total_tokens NULL
output_text_tokens_estimated NULL, estimator_id NULL
source, completeness, input_basis, output_basis
parser_version, evidence_kind, observations_count, updated_at
native_counts_json              # allowlisted numeric usage only
```

No unchecked arbitrary event blob. Model-level breakdown can be added as child
observations if a run genuinely uses multiple models; if that happens before
support exists, mark aggregate model `multiple` and preserve safe per-model numeric
data in a validated structure. Never assign all usage to the requested model blindly.

New `invocation_leases`: run_id PK/FK, provider, acquired_at, released_at.
Index active provider leases and run owners/timestamps. Replace task-only capacity
accounting at all call sites; legacy reservation rows remain historical/read-only
after migration. During rollout, the one service counts any still-active legacy
reservation without double-counting its migrated run. Set configured concurrency
limits transactionally; don't infer capacity from the provider's BUSY flag.

New `orchestration_operations`: id, product_project_id, kind (initially PLAN), state,
expected_plan_revision, current_run_id, attempts, cancel_requested, created_at,
updated_at, finished_at, safe_error_code. A partial unique index prevents two active
PLAN operations for one product. This supports the three planner-format attempts
without pretending they are one provider invocation. Each attempt has its own run.

Index product/run time, mission/run time, active operations and leases. Usage fields
have nonnegative CHECK constraints; absent values remain NULL. Foreign keys and
indexes should be introduced with a migration test over a copy of the existing
schema. No historical output log is copied into the migration.

### Interfaces

Use dataclasses/Pydantic shapes and typed enums, not unstructured `dict[str, Any]`
at new boundaries. Conceptual interfaces:

```python
class InvocationSpec:
    owner: InvocationOwner
    role: Role
    stage: InvocationStage
    prompt: CompiledOrLegacyPrompt
    workdir: Path
    provider: str
    model_requested: str | None
    timeout_s: float
    retry_of_run_id: str | None

class InvocationService:
    async def execute(self, spec: InvocationSpec) -> InvocationOutcome: ...
    async def cancel(self, run_id: str) -> CancelResult: ...
    async def recover(self) -> RecoverySummary: ...

class UsageAccumulator:
    def observe(self, event: ProviderEvent) -> None: ...
    def finalize(self, outcome: ProcessOutcome) -> UsageSummary: ...

class TokenEstimator:
    def estimate(self, text: str, model: str | None) -> TokenEstimate: ...
```

These are proposed interfaces, not runnable code. `InvocationOwner` validates
product/phase/mission/task relationships once. `CompiledOrLegacyPrompt` preserves
current string execution with version `legacy-v1`; the future compiler can supply
blocks without changing the adapter protocol. Provider choice remains in existing
policy until routing work is explicitly implemented.

### Execution algorithm

1. Validate owner IDs and canonical workspace. For product planning, create an
   owned private temporary subdirectory, never shared `/tmp` as writable workspace.
   Logs live under the GG state root with run-specific permissions/retention.
2. Render existing prompt unchanged, measure chars/words/bytes, estimate with
   versioned char4 fallback and construct safe manifest. A tokenizer can be added
   later; don't fetch model tokenizers or make network calls at run time.
3. In one short transaction, create PREPARED run + manifest + initial UNKNOWN
   usage record. Atomically acquire capacity only if provider eligibility and owner
   state remain valid. Otherwise persist WAITING with reason; do not count an
   execution or consume an attempt until a real launch is made.
4. Register cancellation before publishing a runnable process. Set STARTING and
   invoke existing spawn gate. Callback persists PID/PGID/start identity; only then
   release. A failed identity persistence never executes the provider.
5. Observe bounded raw JSON events in memory for typed outcome/usage. Stream only
   normalized redacted text to EventBus. Emit run_id/task_id/product_id so parallel
   activity can be attributed. Write raw logs only under existing sensitive policy;
   new artifacts get restrictive permissions.
6. Accumulate provider-specific usage incrementally, independent of bounded display
   tails. Recognized final totals replace provisional counters, not add to them.
   Track seen event/step/message identities and protocol completeness. Malformed
   usage never turns successful code into failure; mark UNKNOWN/PARTIAL with safe
   parser diagnostic. Malformed task/review output remains a separate role failure.
7. Reconcile explicit terminal event + process exit/cancel/timeout. Intermediate
   tool/step completion is never a final success marker. Persist outcome, observed
   model, duration, usage and evidence references. Update provider reliability once.
   On orchestrator-internal refusal, use existing short internal cooldown, not a
   provider reliability penalty.
8. Release lease only after confirmed process exit/ownership resolution. Publish
   completion after durable transaction. Engine receives normalized result and
   retains responsibility for checkpointing, review and phase transitions.
9. Restart reconciliation scans **all** unfinished run owners, not an inner join
   requiring a mission. Recover/cancel owned processes with current PID-identity
   safeguards, reconcile leases, and expose interrupted plan operation for bounded
   resumption. Do not re-execute completed external side effects speculatively.

For managed planning: capture expected revision before launching; when output
returns, recheck operation ownership, product terminal/cancel state and revision
before storing a plan. Reject stale results. Classify provider failure separately
from schema failure and select eligible fallback under bounded attempt/wait rules.
Do not write a successful health result for a failed adapter response.

### Usage parsing rules

Claude: prefer final `result.usage`; `modelUsage` is corroborating breakdown. Treat
assistant usage as provisional/deduplicated and incomplete without terminal evidence.
Do not add thinking tokens twice. Codex: parse a verified stdout terminal usage
shape when present; record thread ID. Optional exact-session fallback reads only
the matching session metadata/token events, checks owner cwd/time interval and
uses cumulative deltas for resumed sessions. No fallback is required for initial
release if compatibility cannot be demonstrated—show UNKNOWN honestly. AGY:
UNKNOWN until a sanctioned fixture proves a schema; no generic Gemini guess.
OpenCode: unique step-finish deltas, record unfinished steps, include only completed
observations and label completeness. Normalize cache/reasoning basis per version.

Important: reported raw model token totals are counters, not independently audited
provider billing. Claude's reported final fields can carry PROVIDER_REPORTED via
CLI; OpenCode normalized CLI metadata and Codex session counters are CLI_REPORTED.
Local prompt/output text estimation remains a separate metric even on the same run.

### APIs and frontend behavior

Add `GET /api/runs?product_project_id=&mission_id=&task_id=&cursor=&limit=` with
bounded pagination and validated owner filters; `GET /api/runs/{run_id}` for safe
metadata/usage/outcome; `GET /api/runs/{run_id}/context` for safe manifest;
`GET /api/analytics/usage` for grouped numeric usage and coverage. Scope log access
through run ownership; do not accept arbitrary filesystem paths.

Add `POST /api/product-projects/{id}/planning-operations` returning 202 with
operation ID, and `GET /api/operations/{id}`, `POST /api/operations/{id}/cancel`.
Keep legacy `/plan` as a compatibility facade that awaits the same operation and
returns its existing response shape. Duplicate requests attach to the active
operation or return a clear 409; they never create a second planner writer.

The product page uses the new operation endpoint, displays planning progress and
safe failure, and remains cancellable. Mission/provider run rows open RunInspector:
requested/observed model, stage, state, duration, GG prompt size estimate, reported
usage/source/completeness, template/policy and block list. For old runs render
“not captured”; don't render zero. For legacy monolithic prompts say selection
decisions unavailable. Context text download is not enabled by default.

### Migration, compatibility and rollback

Additive nullable columns preserve existing rows and API responses. Backfill no
fictional prompt versions/models/usage; old rows remain legacy/UNKNOWN. New service
writes complete owner fields for new runs. Ensure UI counts both lowercase legacy
and uppercase task states correctly without rewriting historical evidence.

Back up SQLite using a consistent SQLite backup before migration and stop the
single owning backend for deployment. Test migration failure/restart and ensure
script/version recording cannot leave a half-applied ALTER sequence. Do not open
the live history DB via an auditing constructor that auto-migrates it.

Feature-flag compiler behavior separately from observation. Instrumentation can be
disabled for preview/aggregation, but do not fall back to unowned execution when
writing a run/lease fails. Operational rollback drains/stops new invocations, uses
the prior UI or disables new endpoints, and keeps additive evidence tables. An old
binary must not be started while new-format active operations remain; use a tested
schema-compatible rollback version or restore the pre-migration DB only after
explicitly accounting for subsequent completed work. Never auto-drop usage/evidence.

### Required tests and dogfood

Unit fixtures: each provider's success/error precedence, token normalization,
duplicate/intermediate events, missing terminal results, truncated/oversized JSON,
cache/reasoning subsets, non-ASCII estimates and unknown usage. Fixture sanitization
retains numeric metadata only; raw conversations/credentials never enter Git.

Integration: every caller uses the service; product planning failure persists
one failed invocation; concurrent requests respect operation/lease limits; cancel
waits for process exit; restart before and after gate release; shutdown during
planning; version conflict after output; DB write refusal; partial stdout failure;
no double-counting across recover/retry. Use real local subprocess fixtures with
fake provider protocols, isolated temp repos/DBs and no subscription calls.

API/UI: pagination/project isolation, unauthorized access, safe redaction,
legacy null values, partial aggregates with coverage, error rendering,
double-click planning, cancellation and exact run deep links. Existing 96 focused
backend tests and 45 frontend tests are the baseline, not the entire acceptance
suite for new implementation.

After implementation, one owner-authorized small product plan and one existing-repo
mission are sufficient initial real dogfood. Include one intentional fake quota
failure first to test failover without burning real quota. Compare generated
prompts byte-for-byte under legacy policy, verify run counts and usage provenance,
then allow the next increment to change prompts. Do not rerun expensive historical
missions merely to fill charts.

Acceptance criteria:

1. All five call paths create one attributed run before exec and are recoverable.
2. Failed/partial planner calls cannot increment successful_runs as success.
3. Cancellation/leases reflect actual process lifetime.
4. Every new run has measured GG prompt size, version and safe manifest.
5. Supported observed usage is parsed; missing/partial values stay explicit;
   replay/restart never doubles cumulative totals.
6. An operator can identify model, role, owner, outcome, prompt estimate and usage
   provenance from the browser without reading JSON or raw logs.
7. No provider quota is used by tests, no raw secrets persist in manifests, and
   existing mission/delivery behavior changes only where an identified bug requires it.

## R. Git, documentation and verification state

The audit started with a clean worktree on `main`, HEAD `6ebcc7d` (also origin/main).
The only intended changes are Markdown documentation; no core implementation,
config, migrations or tests were changed. Frontend type checking regenerated a
tracked build-info file; it is restored to the exact baseline before commit.

Verification performed: 96 focused backend tests passed in 114.50 seconds; all
45 frontend tests passed; backend ruff and mypy passed (43 source files); frontend
type checking passed. Frontend tests emitted existing React Router future-flag and
TaskLogPanel act warnings. This is not a claim that the complete backend suite or
desktop packaging was re-certified. Pure deterministic probes additionally
demonstrated failed-planner false-success accounting, intermediate-success failure
misclassification, and accepted duplicate criterion IDs. No core fixes were applied.

Exact documentation files changed:

- `README.md`, `AGENTS.md`, `CLAUDE.md`, `ARCHITECTURE.md`
- `DEVELOPMENT.md`, `PROVIDERS.md`, `SECURITY.md`, `TROUBLESHOOTING.md`
- `docs/ARCHITECTURE.md`, `docs/PARALLEL_SCHEDULER.md`, `docs/TASK_DAG.md`,
  `docs/WORKTREE_ISOLATION.md`
- `docs/adr/0002-state-machine-persistence.md`,
  `docs/adr/0003-git-checkpoint-strategy.md`,
  `docs/adr/0004-provider-failure-classification.md`,
  `docs/adr/0005-tauri-desktop-architecture.md`

The documentation commit is recorded in the final handoff; the source baseline
remains the HEAD above. Historical database/evidence were
read only. No provider jobs, shared-instance shutdown, destructive cleanup, push,
or product implementation was performed.

## Final decision: five highest-leverage changes, in exact priority order

1. **One durable, measurable invocation path with truthful outcomes.** It closes
   planner/recovery/accounting gaps and makes real consumption visible. It beats
   building a dashboard first because today's records omit calls and can classify
   failures as successes. Include terminal-event correctness, model/usage provenance,
   prompt manifests and real cancellation/capacity ownership.
2. **A deterministic role-specific context compiler with environment-aware task
   contracts.** Pass architecture, relevant requirements, dependency artifacts and
   exact evidence; remove repeated instructions and unnecessary replanning. It beats
   an LLM Prompt Engineer because GG already owns the needed structured information
   and can select it without another quota-consuming inference.
3. **Immutable attempt/evidence lineage and exact-SHA acceptance with honest review
   provenance.** Preserve retries/findings, rerun criteria explicitly, verify the
   fresh candidate and re-review code repairs. It beats more automatic retrying
   because automation must first know which artifact actually passed what.
4. **Bounded, criterion-targeted repair with environment-first diagnosis.** Repair
   the failing obligation, independently review and recheck it, and stop on unchanged
   failure/budget exhaustion. It beats whole-phase replay and default waivers by
   reducing unnecessary work while preserving the requirement.
5. **An actionable product console backed by honest usage and routing evidence.**
   Show now/why/next/needs-me/verified, scoped repair/recheck/resume controls, run/context
   inspection and measured provider/model consumption. It beats adding buttons or
   speculative model rankings because every action has a clean state contract and
   every comparison has provenance and a denominator. Correct dependent-DAG artifact
   visibility before enabling it as the product workflow's general execution mode.
