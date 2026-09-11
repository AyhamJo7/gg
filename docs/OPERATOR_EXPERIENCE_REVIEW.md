# GG architecture retrospective and operator experience review

Review date: 2026-09-12. Original audit source: `6ebcc7d`; original audit
documentation commit: `c06079ed0c597ab7b2c10fd1eef6bbc646725ea3`.
Runtime reviewed: `649e67ee46f32ec062975125fc8d49946819aff7`.
Implementation branch starts at `f2c17c03b41908021150dcf46f1eb7be539f3de1`.
The latter changes only the repair-worker test harness, not production behavior.

This is a source/diff review plus actual Chromium operation of the Vite UI and
isolated supported fake-provider lifecycle backend. Rare-state UI fixtures are
identified separately below. No production provider invocation, publication,
merge, tag, historical-evidence mutation, or backend semantic change was made.
The earlier real-provider canary is owner-supplied release evidence, not a canary
repeated during this review. Established seals were not reopened without evidence.

## 1. Executive verdict

- Architecture redesign: **SUCCESS**, within the local single-owner contract.
- Frontend strategy: **B — EVOLVE CURRENT UI**.
- Daily operator UX: **7/10 after this branch; 5/10 before**. These are subjective
  judgments from bounded browser use, not a longitudinal usability study.

The original bottleneck really was the contract between stages. The redesign
substantially fixes it: attributable invocations consume scoped context, tasks
consume actual dependency artifacts, reviews inspect identifiable candidates,
and acceptance certifies the candidate that delivery records. Autonomous repair
is now production-executed, not merely an executable library.

The remaining bottleneck is translating this truth into a small number of
operator decisions. Previously, useful evidence existed but finding it required
knowing GG's implementation. This branch makes ongoing work and attention the
entry point while retaining the underlying evidence. It does not make GG a
finished, universally unattended software factory.

## 2. Original recommendations versus reality

The original report is [the 2026-09-10 audit](ARCHITECTURE.md), particularly A, C,
F, J, K, P and Q. History from the original source through the runtime seal was
compared, including actual engine, compiler, provenance, dependency and worker
implementations. Reports were navigation aids, not implementation proof.

Source paths below are relative to `backend/src/orchestrator/`; line references
refer to the unchanged sealed runtime. “Solved” means the original contract gap
has a concrete implementation, not a promise of defect-free behavior.

| Original concern | Actual response | Source evidence | Solved? | Remaining weakness |
|---|---|---|---|---|
| Invocation accounting | Common durable run/spec/service and terminal reconciliation | `invocations.py:104,218,426`; `usage.py` | Yes | A completed CLI protocol is not a successful product task; correctly separate |
| Planning integrity | Product planner uses attributed invocations and durable PLAN operation | `project_engine.py:339,447`; `operations.py` | Yes | Browser planning request remains a long action; operation UX is incomplete |
| Provider capacity ownership | Run-owned leases across execution owners | `invocations.py:247`; `reservations.py` | Yes locally | One backend owner; not a distributed semaphore |
| Process recovery | Identity-aware PID/PGID capture, owned cancellation, terminal recovery | `invocations.py:304,338`; `process.py`; `orphans.py` | Yes | OS/process failure remains an operational concern, not solved by UI |
| Deterministic compiler | Role policies, budget selection, manifests and strict failures | `context_compiler.py:957,1150,1468` | Yes | GG prompt estimates are not total CLI context or actual billed tokens |
| Requirement mapping | Scoped mapped requirements and fail-closed missing mappings | `context_compiler.py:318,512,1350` | Yes | Correct mapping still depends on plan quality |
| Architecture propagation | Stack plus relevant decisions projected into task context | `context_compiler.py:362` | Partial | Lexical matching, capped selection and preferred blocks do not guarantee every relevant decision survives |
| Dependency context | Explicit summaries and artifact references | `context_compiler.py:415` | Yes | Summary quality remains bounded by upstream output |
| Dependency filesystem truth | Base/result pins, deterministic multi-parent artifact, ancestry checks | `dep_inputs.py:99,142,383`; `parallel_engine.py:1552,1866` | Yes | Genuine merge conflicts still need action |
| Parallel sibling isolation | Roots use pinned DAG base, not moving integration HEAD | `parallel_engine.py:1727`; `dep_inputs.py:142` | Yes | Workspace scopes are scheduling hints, not provider sandboxes |
| Review contamination | Reviewer policy excludes implementer self-assessment | `context_compiler.py:512,957` | Yes for GG packing | Cannot police every file a CLI independently reads |
| Writer provenance | Contribution-based ledger, explicit adoption, unknown external identity | `provenance.py:150,202,256,483` | Yes | Unknown history must remain unknown; operator adoption is a meaningful claim |
| Review independence | Excludes complete contributing provider writer set for reviewed range | `provenance.py:539,623` | Yes | Provider-name independence is not a guarantee of statistically independent reasoning |
| Exact-SHA review | Reviewed base/head and repo-scoped review attempts | `provenance.py:623,800` | Yes | A later change deliberately invalidates old review |
| Criterion evidence | Immutable attempts, plan binding, explicit recheck | `project_engine.py:1506,1614`; `provenance.py:800` | Yes | Weak criteria still certify weak requirements |
| Fresh-checkout evidence | Exact checkout, toolchain execution and executable criterion replay | `project_engine.py:2011`; `provenance.py:800` | Yes | External environments and credentials are not reproduced magically |
| Delivery fencing | Re-read HEAD/cleanliness and reevaluate evidence under lock, post-write check | `project_engine.py:1930` | Yes | Local advisory locks are not exclusive control over unrelated external writers |
| Restart behavior | Durable stage/claim/attempt recovery, no repeated completed work | `invocations.py:338`; `repair.py:1169`; `repair_worker.py` | Yes in tested contracts | Normal and resumed code paths require careful synchronized maintenance |
| Repair boundedness | Implementation-defect classification, hard budgets, repetition/no-change/oscillation stops | `repair.py`; migration `0015` | Yes | Refusal on ambiguity is intentional, not insufficient autonomy |
| Repair exact-SHA semantics | Pinned scope/result, ancestry, exact review and recheck | `repair.py:838,898`; `repair_worker.py` | Yes | Successful cycle returns to acceptance; never itself delivery |
| Repair reviewer independence | Reviewer outside artifact writers, including earlier repair writers | `repair.py:898,1169`; `provenance.py:539` | Yes | May wait when every available provider contributed |
| Provider quota safety | Availability/cooldown/leases and bounded attempts; honest unknown telemetry | `invocations.py`; `usage.py`; `config/orchestrator.yaml` | Yes for local policy | Actual subscription quota remaining is not known |
| Operator visibility | Runs/context/evidence/repair APIs and corresponding panels | `api/app.py`; frontend inspectors | Partial → improved here | No complete cross-stage activity feed; latest 50 product runs only |
| Frontend usability | Existing panels preserved and recomposed around operator work | `frontend/src/pages/OverviewPage.tsx`, `ProductOverview`, `operator.ts` | Partial → improved here | Plan editing, deep recovery and long history still expose internals |

### Actual current execution spine

```text
Idea → product + plan revision → durable planning invocation
     → scoped phase mission → compiled role context → leased CLI invocation
     → committed contribution → exact independent review → verification
     → criterion attempts + fresh-checkout replay → delivery fence → DELIVERED

Parallel mission: pinned base → dependency artifact → task worktree → result
                 → ancestry-covered integration → review → verification

Failed acceptance: deterministic classification → bounded durable repair cycle
                  → worker claim → repair → independent review → exact recheck
                  → canonical acceptance (never direct worker delivery)
```

Product roadmap phases still become sequential missions. A standalone Parallel
Safe mission has the DAG/artifact execution path; do not imply every product
roadmap automatically becomes a parallel DAG.

## 3. Architectural strengths, ranked

1. **Artifact truth.** Dependency content and acceptance evidence now share a
   concrete Git identity. This removes entire classes of plausible-but-wrong
   success, rather than merely improving prompts.
2. **Bounded production repair.** Durable cycles, worker claims and exact rechecks
   turn proven implementation defects into safe additional work, without an LLM
   deciding whether it may weaken the acceptance contract.
3. **Unified execution ownership.** Planning no longer evades run accounting,
   capacity, cancellation or recovery.
4. **Deterministic context with observable omissions.** Missing mandatory
   contracts stop execution; independent review is not fed self-congratulation.
5. **Adversarial tests around real boundaries.** Git ancestry, restart, writer
   sets and evidence scope receive concrete tests, not only mocked happy paths.

## 4. Remaining architectural weaknesses, ranked

No new HIGH/CRITICAL sealed-correctness contradiction was demonstrated.

| Severity | Weakness and evidence | Practical response |
|---|---|---|
| MEDIUM | Architecture relevance uses keyword overlap and a limited selection, `context_compiler.py:362`; known lexical limitation is explicitly documented | Preserve current seal; use real omitted-decision examples before proposing explicit mappings |
| MEDIUM | Normal repair and restart continuation duplicate substantial validation/transition sequencing, `repair.py:898,1169` | When a real change touches both paths, extract tested stage predicates; do not reorganize the state machine now |
| MEDIUM | Cross-module coordinator knowledge and dictionary/string states make transitions hard to audit, `project_engine.py`, `parallel_engine.py`, `repair_worker.py` | Small typed boundary records and transition ownership documentation, only alongside necessary changes |
| MEDIUM | Usage observation is bounded: log read under 8 MB and last 4,000 lines, otherwise bounded observed stream, `invocations.py:707` | Treat usage completeness conservatively; do not compare partial counts as whole-run consumption |
| MEDIUM | Product resume clears product pause but does not resume paused phase missions, `project_engine.py:1293,1308` | Explain separate control levels and link the mission's existing Resume action; no silent semantic change |
| LOW | Run/manifest/usage and some lifecycle writes commit in separate DB calls | Recovery/fail-closed behavior is essential; no evidence here justifies a wholesale transaction rewrite |

### Context and telemetry reality

The deterministic fixture benchmark (`uv run python scripts/compare_context.py`
from `backend`) produced these **char4 estimates**, not actual provider usage:

| Fixture | Legacy estimate | Compiled estimate | Delta |
|---|---:|---:|---:|
| Small/trivial | 127 | 317 | +190 |
| Medium implementation | 186 | 322 | +136 |
| Planner | 1,013 | 1,170 | +157 |
| Review | 381 | 361 | −20 |
| Repair | 86 | 371 | +285 |
| Dependency-heavy task | 79 | 417 | +338 |
| Long historical repair | 534 | 371 | −163 |

Mandatory blocks survived in all seven fixtures. The increase in short legacy
prompts is often restored requirements/safety, not waste. The compiler fixes
packing integrity; it has not proved universal subscription-token reduction.
Residual objective/title/description repetition exists in candidate construction.
Do not add a per-run Prompt Engineer LLM to solve this deterministic problem.

`config/orchestrator.yaml` enables all four adapters; OpenCode is pinned to
`opencode-go/muse-spark-1.3-contributor`, `xhigh`; the others inherit CLI model
defaults. This is configuration, not an assertion of current account health.

| Provider | What current code can honestly record |
|---|---|
| Claude | Final structured result usage when emitted, CLI_REPORTED; cache counts retain their semantics; provisional assistant events do not become a fabricated total (`usage.py:76`) |
| OpenCode | Structured step usage, often CLI_REPORTED / PARTIAL / PER_STEP; not automatically whole-run input (`usage.py:148`) |
| Codex | Parse recognized stdout token events when present; otherwise UNKNOWN. A cumulative-snapshot helper does not mean GG universally discovers sessions (`usage.py:245,282`) |
| AGY | UNKNOWN; production invocation explicitly marks telemetry not captured (`usage.py:328`, `invocations.py`) |

No missing count is converted to zero, no local prompt estimate is presented as
exact usage, and no remaining quota or monetary bill is invented. The owner's
two-action canary is useful bounded evidence, not a provider effectiveness study.

## 5. Complexity and maintainability

Most additional persistence is justified: a run, phase attempt, dependency input,
writer contribution, evidence attempt, repair cycle and worker claim answer
different questions. Combining them into one generic job would erase useful
truth. Conversely, the total state space now exceeds what an operator should see.

The complexity risk is **duplicated transition reasoning across coordinators and
recovery**, not raw file length. The engines still combine provider selection,
Git, prompts, persistence, review and recovery, with broad dictionary types and
some private helper dependencies. A rewrite would risk well-tested invariants.
Make future maintenance changes against paired normal/restart contract tests.

Schema evolution is append-only and historical unknown evidence remains
non-certifying. SQLite/WAL and one local daemon remain appropriate. Tests are
strong on safety boundaries but increasingly expensive to set up; the fake
lifecycle harness itself can encounter the correct unknown-dirt gate when its
bootstrap writes are uncommitted. That is not proof the production invariant is
wrong. The test-only Git stat-cache fix is accepted and not reopened.

## 6. What should now be left alone

- Invocation lifecycle, spawn ownership and terminal precedence.
- Compiled-mode fail-closed behavior and reviewer contamination policy.
- Contribution-based writer sets, explicit adoption, repo-scoped evidence.
- Pinned DAG artifacts, sibling isolation, ancestry coverage and stale lineage.
- Repair classification, budgets, oracle protection and restart claims.
- Canonical acceptance and delivery fencing.
- Single local backend/SQLite deployment and honest UNKNOWN usage.

Change these only for reproduced defects or concrete dogfood requirements. Do
not start adaptive routing, self-learning prompts, session reuse, distributed
workers, task-level autonomous repair, or Increment 5 as a side effect of this work.

## 7. Browser UX audit

Before editing, I operated the real browser UI against the repository's isolated
fake-provider backend and inspected the rendered pages at 1440×900 and
1920×1080. The initial root route was Mission Control: a mission selector and
execution internals, not a workspace decision surface. A newcomer had to learn
the difference between repository projects, product projects and missions before
choosing where to start.

Product pages defaulted to plan detail while the product was operating. Repair
and evidence were buried in delivery. Mission links did not reliably select the
intended mission. A missing context/evidence load could appear empty rather than
unavailable. Technical identity was frequently more prominent than the work.

These are information-architecture problems, but not grounds to discard the
working React components. Their evidence, gates, findings, logs and controls are
valuable. The branch changes entry points and hierarchy, not GG's truth model.

## 8. Daily operator scorecard

| Dimension | Before | After | Reason |
|---|---:|---:|---|
| Clarity | 5 | 8 | Attention, automatic repair and terminal status separated |
| Speed | 6 | 7 | Direct links and default operations view; history still shallow |
| Trust | 7 | 8 | Exact evidence retained; missing/unknown not promoted to pass |
| Control | 6 | 7 | Existing controls surfaced with confirmations and errors |
| Recoverability | 5 | 7 | Gates, repair stops and separate resume levels visible |
| Information density | 5 | 7 | Desktop columns and progressive detail; some long panels remain |
| Visual quality | 6 | 7 | Consistent spacing, contrast and focus, not a wholesale reskin |
| Learnability | 4 | 7 | Two clear entry workflows, fewer identifiers first |
| Overall | 5 | 7 | Useful daily cockpit, still expert-oriented at difficult boundaries |

Scores describe observed operator utility, not a measured aggregate or a claim
that weeks of real-product operation have been tested.

## 9. Frontend strategy decision

**B — EVOLVE CURRENT UI.** The route structure and components are recoverable.
Keep exact inspectors and action contracts; add a workspace overview, a product
operations default, explicit attention routing, and safer live state handling.
A rebuild would spend effort recreating useful evidence controls and increase
regression exposure without a demonstrated benefit.

## 10. UX design direction

The pre-implementation direction was:

- Navigation: Overview → Products / Missions / Repositories, with Providers,
  Priority Matrix and Analytics retained.
- Dashboard/attention: decisions first, ongoing work second, outcomes third;
  no ornamental KPIs or unsupported confidence/quota percentages.
- Product: status and next action, active phase missions, repair, then trust;
  persistent section URLs for plan, roadmap, missions, human actions, delivery,
  activity.
- Timeline: use existing run records for a bounded activity list; do not invent
  a complete lifecycle timeline from timestamps that lack event semantics.
- DAG: task names, dependencies, state, provider and blocker first; exact inputs
  and results below.
- Repair/Human Gates: explain why, what is happening, what remains, and what the
  operator should do. Do not equate successful repair with delivery.
- Runs/providers/evidence: summary → evidence → technical identity. Preserve
  unknown telemetry, bounded logs, exact SHA and provenance.

This is implemented through existing APIs. There are no presentation API additions.

## 11. Implementation state

Branch: `feature/operator-experience-v1`, based exactly on `f2c17c0`.
Frontend commit: `6342621` (`feat(ui): surface operator decisions and artifact trust`).
Frontend changes and the browser regression script are committed separately from
documentation synchronization. See the final handoff for immutable commit SHAs;
this report deliberately does not embed its own self-referential commit hash.

No backend source, migration, configuration, provider adapter, dependency lockfile
or sealed test file changed. README and the current architecture map have stale
claims corrected. AGENTS.md and CLAUDE.md remain unchanged: zero additional
automatically injected instruction context.

Exact frontend/script files changed (prefix `frontend/src/` unless stated):

```text
components/AppShell.tsx                 components/DagGraph.tsx
components/EvidencePanel.tsx            components/GateCard.tsx
components/PageError.tsx                components/ProductActivity.tsx
components/ProductOverview.tsx          components/RepairCyclePanel.tsx
components/RunInspector.tsx             components/TaskPanel.tsx
lib/api.ts                             lib/hooks.ts
lib/operator.ts                        lib/types.ts
main.tsx                               styles.css
pages/LifecycleDetailPage.tsx           pages/LifecyclePage.tsx
pages/MissionControlPage.tsx            pages/NewMissionPage.tsx
pages/OverviewPage.tsx                  pages/PriorityPage.tsx
pages/ProjectsPage.tsx                  pages/ProvidersPage.tsx
test/DagGraph.test.tsx                  test/EvidencePanel.test.tsx
test/LifecycleDetailPage.test.tsx       test/MissionControlPage.test.tsx
test/NewMissionPage.test.tsx            test/OverviewPage.test.tsx
test/ProjectsPage.test.tsx              test/operator.test.ts
test/polling.test.tsx
/scripts/operator-experience-browser.mjs (repository-relative)
```

Documentation files: `README.md`, `ARCHITECTURE.md`, and this report.
AGENTS.md remains 3,812 bytes / 481 words; CLAUDE.md remains 573 bytes / 66 words.
No generated build artifact, token or screenshot is committed.

## 12. Dashboard

Before: mission-centric root with no cross-product decision queue.
After: Overview lists attention, ongoing/ready work, recent outcomes and provider
availability, with explicit “Build a product” and “Work on a repository” links.
Mission stops remain separately visible because the list API cannot reliably
deduplicate all mirrored product gates. Active repair suppresses a generic
blocked-product alarm, but never hides an active human gate.

Verdict: materially better. Repair detail fan-out is bounded to 20 active products
and incomplete coverage is disclosed; this is not a scalable global inbox API.

## 13. Project detail

Before: plan-first view with repeated technical information.
After: operations-first overview, human-facing state, current phase links,
repair and trust side by side; section selection survives URL navigation.
Generation/recheck controls do not compete with a known active repair. Product
pause/resume is available, with explicit guidance that a paused phase mission
must be resumed separately. Cancellation retains evidence but no longer looks
like an active gate merely because historical gate rows remain open.

Verdict: coherent high-level narrative. Complex plan revision still requires
structured JSON; that is a remaining usability gap, not silently solved here.

## 14. Operator attention experience

Before: open each project/mission to discover stops.
After: product decisions and mission stops have direct links from Overview.
Unavailable updates are explicit, not an empty healthy state. Failed products
remain attention-worthy; cancelled products do not.

Verdict: a useful local inbox, not a fabricated deduplicated count of unique
operator obligations. There is no dismiss/snooze workflow pretending a backend
gate has been resolved.

## 15. DAG experience

Before: clickable non-semantic cards with raw task IDs and SHA snippets prominent.
After: native keyboard-operable task buttons, named dependencies, dependency
stages, stale-input explanation, provider and blocking text. Exact SHA remains in
TaskPanel. Tasks in one column may be dependency-independent but still compete
for provider capacity and file locks; the UI says so.

Verdict: improved small/medium DAG comprehension. Large graph search, zoom and
hundreds-of-task performance were not validated; no graph library was added.

## 16. Repair UX

Before: repair ledger hidden within delivery, exposing cycle/state terminology.
After: repair on product Overview, attempts used/remaining, repair → independent
review → exact recheck, provider, stop reason, automatic wait guidance and
confirmed cancellation. Polling refreshes stages without navigation; lineage and
run IDs remain available below.

Verdict: defining capability is finally visible. Criterion IDs still sometimes
stand in for a human-readable defect title, and complete historical cycles can
grow long. No “repair everything” button bypasses classification or budgets.

## 17. Human Gate UX

Before: useful underlying why/what/where fields, but poor discoverability and
weak action feedback.
After: prominent routing from product and workspace; explicit adoption
confirmation, action errors, input labels, and preserved names-only credential
guidance. Cancelled products show historical gates without active resolve controls.

Verdict: better safety and discoverability. Product and mission gates remain two
surfaces because their actions have distinct contracts. The UI does not collect
credentials or automatically adopt unknown changes.

## 18. Provider UX

Before: a technical table; installation testing could be read as health proof.
After: availability on Overview; “Check installation” wording explains it does
not verify authentication/quota, zero-run success is “No runs,” observed success
shows its denominator, and cooldown is eligibility to retry, not a known reset.

Verdict: honest and serviceable. Provider table density and narrow-screen overflow
remain secondary polish. Configuration enabling a provider does not prove it is
healthy. No real CLI authentication or quota check was performed for the audit.

## 19. Run/log UX

Before: raw live output dominated Mission Control and inspectors could stale.
After: logs/handoffs are expandable; run status uses authoritative run status,
summary precedes identity, inspector updates while open, context resets when the
run changes, and mission selection is URL-addressable. Unknown mission links do
not silently open an unrelated mission. Product Activity includes planning runs.

Verdict: less noisy and easier to trace. Context remains metadata/omissions, not
a raw prompt viewer. Activity is capped at 50 and is not a complete event timeline.

## 20. Evidence/trust UX

Before: detailed ledger-first output, with an unhandled draft response shape.
After: authorship, independent review, verification, criteria and fresh checkout
are summarized, followed by expandable exact evidence. A draft with no target
candidate says “No candidate yet”; absent checks are pending, not automatic
failure. Failed loads cannot masquerade as a clean result. No confidence score.

Verdict: trust is now legible without abandoning exactness. Candidate evidence
and historical delivery remain distinct concepts; changing the current checkout
does not retroactively mean a historical record never existed.

## 21. Browser validation

Runtime: supported `scripts/lifecycle-fake-backend.py` on loopback 8787 with a
fresh disposable SQLite database/product root, plus the real Vite frontend on
5173. Chromium via Playwright. Only the review-owned processes were used.

**Actual backend/UI flow:** Overview → new product → generate plan → inspect plan
→ start → Human Gate → pause/resume product → exact mission link → explicit
adoption of fake bootstrap changes → resolve mission gate → inspect attributed
run and context → confirmed product cancellation retaining history.

**Explicit browser API fixtures:** repair in progress, exhaustion, success,
provider wait, external-credential gate, stale evidence, acceptance/delivery,
parallel DAG with running/completed/blocked tasks, dependency conflict, stale task,
review finding, task logs, unavailable provider and cooldown. Repair state changes
and stale-task changes were observed through polling without page reload. Native
keyboard Enter selected a DAG task. These fixtures validate UI behavior, **not
new proofs of backend repair, integration or delivery correctness**.

Screenshots were captured at 1440×900 and 1920×1080, including light appearance.
They stay outside Git under the disposable review directory. The reproducible
script is `scripts/operator-experience-browser.mjs`; it refuses non-fake providers
and requires a temporary review-token path. The test-only auth bridge uses the
existing desktop token interface; production auth is unchanged.

Limitations: no new real-provider canary, no real generated app delivered in this
audit, no destructive conflict resolution experiment, no large-history endurance
benchmark, and no long-duration subscription availability study. Review rejection
is covered by the existing findings/self-review tests and a finding UI fixture,
not a new production reviewer invocation. Plan regeneration and every advanced
waiver control were not exercised end-to-end.

## 22. Accessibility

The browser script runs axe WCAG 2 A/AA and 2.1 A/AA against the full document on
22 captured states. The final completed run reported **zero violations and zero
JavaScript exceptions**. Contrast issues found on the first pass were corrected,
including danger text and primary-button hover. Added skip link, visible keyboard
focus, field labels, semantic task buttons, status text beyond color and native
confirmation dialogs. No new custom modal or focus trap was introduced.

This is automated coverage plus a keyboard DAG check, not screen-reader
certification. Long-table reading order and all browser/assistive-tool combinations
remain outside the tested scope. Existing Priority Matrix reordering remains
drag-only; axe does not detect that keyboard usability gap. Axe is installed only in temporary review tools,
not added to the production dependency tree.

## 23. Test matrix

| Check | Result / scope |
|---|---|
| Frontend unit/integration tests | **82 passed, 22 files**; added attention, draft evidence, exact navigation, action, cancellation and polling regressions |
| Standalone changed behavior tests | **14 passed**: new mission, product detail, repository controls, polling |
| TypeScript / production build | Passed; build is local-only because Vite embeds the local token |
| ESLint | 0 errors; existing Badge fast-refresh warning remains |
| Backend targeted regression | **51 passed**: run APIs, lifecycle API, invocations, context fail-closed, dependency migration, production repair worker |
| Context comparison | Seven deterministic fixtures; all preserve mandatory blocks; estimates above |
| Browser + axe | Actual flow plus explicit rare-state fixtures; 22 document scans clean |
| Backend diff | Empty; no production/test/migration/config changes |

Backend command (from `backend`):

```bash
uv run pytest -q tests/test_run_apis.py tests/test_lifecycle_api.py \
  tests/test_invocations.py tests/test_context_fail_closed.py \
  tests/test_migration_dependency.py tests/test_repair_worker.py
```

Frontend commands: `npm run typecheck`, `npm run lint`, `npm run build`,
`npm run test -- --run`. Existing React Router future warnings, TaskLogPanel
test act warnings and Starlette/httpx deprecations were observed; no failure was
attributed to the sealed test-harness issue. The full 594-test backend suite was
not redundantly rerun for this frontend-only change.

## 24. New findings by category

- **Backend correctness:** no newly demonstrated contradiction of the sealed
  invariants. Resume scope and bounded usage capture are documented limitations,
  not reasons to reopen the release-smoke flake.
- **Frontend functionality, fixed:** draft evidence response crash; wrong mission
  routing; stale cross-entity polling; invalid manual DAG creating a mission before
  local validation; active repair and cancelled-product headline precedence.
- **UX, fixed/improved:** absent workspace attention, plan-first project landing,
  repair buried in delivery, raw identity/log dominance, installation/quota ambiguity,
  uncaught action failures and unsafe cancellation expectations.
- **Performance:** polling is keyed, late responses ignored, scheduled reads do
  not overlap a slow read; dashboard repair fan-out bounded. No large-history
  performance claim. More polling is a deliberate local tradeoff until real
  evidence warrants an aggregate read endpoint.
- **Maintainability:** normal/recovery sequencing duplication, weakly typed
  coordinator boundaries, and contradictory current documentation. Documentation
  corrected; sealed implementation left intact.

## 25. Does anything require another backend increment?

**NO.** No evidence from this review requires a new architecture increment before
shipping the frontend improvements. Small read-only aggregation/history APIs may
eventually improve scale and navigation, but were not needed to make this pass
valuable. Do not convert every UI limitation into a new state machine.

## 26. Was the redesign worth it?

**Yes.** It replaced suggestive execution success with a defensible chain from
invocation to artifact to evidence. The production repair worker and dependency
artifact changes are especially consequential. The price is more state and more
careful recovery maintenance; most of that price buys real correctness.

What was only partly solved: architecture relevance, demonstrated token savings,
operator discoverability, long-history navigation and explainable cross-stage
failures. Do not confuse correct context packing with proof of better model
performance on every task.

## 27. Is GG a coherent product yet?

More coherent now: the browser has a workspace entry point, clear workflows and
an operations-first product page. Difficult boundaries still feel like a control
plane. I would use it daily for bounded engineering work, with explicit plan and
delivery inspection; I would not claim it is ready to leave arbitrary product
ideas unattended indefinitely.

Three largest daily frictions: structured plan editing; multi-level recovery
(product/mission/gate/repair); finding older or cross-stage evidence.

Three strongest differentiators: real artifact-aware parallel execution;
independent exact-candidate review/acceptance; subscription-backed bounded repair
with durable attempts and honest telemetry. None depends on decorative AI claims.

## 28. Top three next actions

1. **Independent frontend review, then ship this bounded branch.** Review attention
   precedence, action contracts, polling identity and accessibility. This realizes
   existing backend value with much less risk than another architecture increment.
2. **Use a few real products and record operator interventions.** Track where the
   operator looked, what was unclear, why GG stopped, and whether recovery was
   possible. This beats speculative routing or token-learning infrastructure.
3. **Address the most frequent observed plan/history friction.** Likely candidates
   are structured plan revision and searchable/paginated evidence navigation.
   Pick from real intervention records; keep the backend semantics unchanged.

## 29. Final recommendation

**SHIP TARGETED UX IMPROVEMENTS AFTER AGY REVIEW.** No AGY invocation was launched
in this review. Leave the branch unmerged, unpushed and untagged.
