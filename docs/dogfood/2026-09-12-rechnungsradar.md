# GG Orchestrator — RechnungsRadar Dogfood Review

Independent senior-engineer evaluation. Read-only against RechnungsRadar; GG changed only in `CLAUDE.md`.

---

## 1. Executive Verdict

```
GG dogfood verdict:            MIXED
Would GG beat manual coordination?  ROUGHLY EQUAL
Would you personally use GG?        YES, WITH SUPERVISION
```

One sentence: **GG's evidence and provenance layer held up impressively against a real, messy, multi-branch repository — and its context layer silently threw away most of the work it paid for.**

The artifact that came out is good. The process that produced it wasted roughly a third of its provider time, discarded a 44,000-character audited plan down to a dangling JSON brace, handed the final reviewer 131 characters of context for a 187 KB diff, and lost three real defects that GG itself had found two days earlier — all under a `COMPLETED` verdict.

---

## 2. Exact Review Window

Machine local time at review start: **Sat 2026-09-12 13:25:41 CEST**.

```
Window start:  2026-09-11 13:25:41 CEST   (2026-09-11T11:25:41Z)
Window end:    2026-09-12 13:25:41 CEST   (2026-09-12T11:25:41Z)
```

All GG and Git activity in that window falls inside **2026-09-12 02:14:47 → 03:05:31 CEST** (00:14:47 → 01:05:31 UTC). Nothing else happened in either repository during the remaining 23 hours.

Timestamps below are **CEST** unless marked otherwise.

---

## 3. Repository Identity

### GG

| Item | Value |
|---|---|
| Path | `/home/adam/projects/gg` |
| Branch | `main` |
| HEAD at review start | `1427e908f1ceae6dddb8e2580808ca3f96d6d7ab` (matches the stated last-reviewed state) |
| HEAD after this session | `3c405506e1d49398322a7734954eb9ae4b2983ab` |
| Working tree | clean before and after |
| Pushed / tagged | no |

### RechnungsRadar

Located from GG's own `projects` table (`id=2004f66f420e4cd6`, registered 2026-09-10T02:23:01Z), not by guessing.

| Item | Value |
|---|---|
| Path | `/home/adam/projects/business/RechnungsRadar` (also via the `~/projects/personal` symlink) |
| Detected type | `python+node` |
| Branch | `integrate/phase9-into-self-serve` |
| HEAD | `bf39469b3d1443608b6fe44ae2e9f174c39ed650` |
| Mission base | `2a20ca71651c946d3ec2b909a5a8dec3cd4c8edf` |
| Working tree | clean (0 modified, 0 untracked) |
| Remote | `origin` → `github.com/AyhamJo7/RechnungsRadar.git` |
| `origin/main` | `80be05f` (2026-07-05) — **none of this work is pushed** |
| Worktrees | main + 1 `.claude/worktrees/` + 4 prunable `.factory/worktrees/` (all pre-GG tooling residue, untouched by this run) |
| Tags | one, `manager-checkpoint-20260901-231907`, unrelated |

**Not modified during this review.** The only commands run against it were read-only inspection plus the repo's own deterministic checks (`pytest`, `ruff`, `mypy`, `pnpm test`) with `--frozen --offline` and `-p no:cacheprovider`.

---

## 4. CLAUDE.md Update

```
previous size:  573 bytes /  66 words
new size:      1655 bytes / 203 words
commit:        3c405506e1d49398322a7734954eb9ae4b2983ab
files changed: CLAUDE.md only (verified: git status clean afterwards)
```

Kept the original shape — a pointer file, not a report. Added one `## Phase` section and one link.

| Added concept | Why it belongs here and not in AGENTS.md |
|---|---|
| Increments 1–4 + Operator Experience v1 are **sealed foundations** | AGENTS.md states the *invariants*; it never states that they are closed. A fresh session reading `repair.py` or `provenance.py` has no way to know it is looking at settled ground. |
| GG is in **real-world dogfood / stabilization** | Answers "what phase are we in?", which is the first thing a session must know and the only thing no source file records. |
| Do not reopen sealed architecture without **reproduced evidence** | The specific failure mode this file prevents: a session reading `docs/ARCHITECTURE.md` and re-proposing work already built. |
| Do not start **speculative Increment 5** | Names the exact wrong move, so it cannot be rationalised. |
| Dogfood evidence drives work | Converts "don't build" into "here is what does justify building". |
| Preserve exact-SHA / reviewer-independence / dependency / repair guarantees | One-line restatement so a session cannot casually weaken them; the detail stays in AGENTS.md. |
| Truthful backend/frontend boundaries; never fabricate quota, confidence, ETA, evidence; never render unknown as zero | The one rule that is enforced in code but was not written down for Claude anywhere. This dogfood found a live violation (§19). |
| Finite provider quota; real providers only when scoped | Present before but as a trailing clause; promoted because this dogfood burned Claude's 5-hour window from 0 % to 62 %. |
| Link to `docs/OPERATOR_EXPERIENCE_REVIEW.md` | It is now a primary orientation document, not an archived report. |

No stale SHA claims, no historical summary, no contradiction with AGENTS.md or ARCHITECTURE.md (checked line by line against both).

---

## 5. What RechnungsRadar Was Supposed to Become

There is **no product-lifecycle record** — `product_projects` is empty. This was run through **Repositories → New Mission**, so there is no plan revision, no requirement rows, no acceptance criteria, and no phases. The entire specification is one free-text mission objective of **12,527 characters**.

Condensed, it asked for:

1. Audit the repo against reality, produce a gap matrix, then **execute** it (explicitly: "do not stop after a roadmap").
2. Turn an XRechnung validator into a full **RechnungsManager** — incoming/outgoing, structured + PDF/scan, email/upload/bulk/portal ingestion, invoicing, partners, search, dedup, review, ERP export, archive.
3. Build a **supplier-portal connector framework** (APIs, email forwarding, assisted download, browser capture) with honest status/provenance.
4. Keep deterministic financial logic separate from AI; every extracted field carries source + confidence; never invent amounts.
5. Complete the end-to-end business workflow and polish the German UI.
6. Finish DATEV/Lexware/SAP B1/Peppol integrations and self-serve billing.
7. Harden tenant isolation, uploads, secrets, GoBD/GDPR controls.
8. Full review + unit/integration/e2e/regression testing, **run the app in a real browser**.
9. Produce a pilot dataset and checklist for a Hamburg restaurant UG.

Plus an output contract of nine report sections and an explicit stop condition.

That is a multi-week program stated as one mission. **GG accepted it verbatim and never decomposed it.** That single fact explains most of what follows.

---

## 6. 24-Hour Timeline

Combined Git + GG. Every timestamp is from the database or Git; none inferred.

```
02:14:47  MISSION_CREATED  8344d7d939924a75 "Retry: e-invoicing market launch ready"
          retry_of 44cdae6dbf4d45c5 · SEQUENTIAL · AUTONOMOUS · profile=balanced
          (operator action — the only one in the window)
02:14:47  ANALYZING → PLANNING · PROVIDER_SELECTED claude
02:14:47  run-b9f788e12af4  claude/planning starts   (base 2a20ca7, tree clean)
02:24:45  planning ends (598 s) · SUCCEEDED · no commit · 43,961-char plan returned
          claude 5-hour quota window: 0 % → 16 %
02:24:45  IMPLEMENTING · PROVIDER_SELECTED opencode
02:24:45  run-7d1c04e9f0cd  opencode/implementation starts
02:24:56  opencode stdout goes silent (19 events: 3 bash, 7 read, 0 writes)
02:32:10  implementation ends (446 s) · SUCCEEDED · exit 0 · no further telemetry
02:32:10  GIT_CHECKPOINT_CREATED  d2565e1  "agent(opencode): implementation checkpoint"
          34 files, +2,661 / −15   ← the largest code contribution of the run
02:32:11  TESTING · PROVIDER_SELECTED codex
02:32:11  run-7f680deb08c8  codex/testing starts
02:38:57  codex FAILS · RATE_LIMIT ("try again at 6:37 AM")
          118 command executions + 2 web searches performed and discarded
          tree unchanged · no commits · no cooldown recorded
02:38:57  PROVIDER_SELECTED claude (failover, attempt 2, identical prompt hash)
02:38:57  run-78e5900bdffb  claude/testing starts
02:45:36    commit 81b7f9e  merge: Phase 9 ingestion hardening → self-serve line
                            (31 files, +2,214 / −166; pulls in b2c3fff)
02:50:25    commit ae5ea6a  fix(web): make the container health probe capable of failing
02:53:10    commit b494b6c  fix(money): kaufmännische Rundung on VAT and DATEV amounts
02:57:47    commit bf39469  docs: launch-readiness audit (297 lines)
                            branch integrate/phase9-into-self-serve created here
02:58:05  testing ends (1,147 s) · SUCCEEDED · 89 Bash + 3 Write
          claude 5-hour quota window: 25 % → 62 % ; 7-day 40 % → 43 %
02:58:05  REVIEWING · PROVIDER_SELECTED codex  ← 19 min after codex declared exhaustion
02:58:05  run-9197c7cb53bf  codex/review starts
02:58:10  codex FAILS · RATE_LIMIT after 5 s
02:58:10  PROVIDER_SELECTED agy (failover, attempt 2, identical prompt hash)
02:58:10  run-459f988cd722  agy/review starts
          stderr: "root agent idle; waiting for 2 background task(s)"
03:05:21  review ends (431 s) · SUCCEEDED · num_turns=1 · ONE MEDIUM finding
03:05:21  REVIEW_RECORDED  reviewer=agy  independent=0
          degradation_reason="writer provenance incomplete (cannot certify independence)"
03:05:21  REVIEW_FINDING_CREATED  MEDIUM  /ingest/capture missing upload rate limit
03:05:21  FINAL_VALIDATION
03:05:21→03:05:31  6 toolchain commands, all PASS (10.3 s total)
03:05:31  MISSION_COMPLETED · git_head bf39469b
—
no human gates · no repair cycles · no restarts · no operator action after 02:14:47
```

Prior context (outside the window, for continuity): mission `44cdae6d` ran 2026-09-10 04:23–05:16 CEST, ended **UNVERIFIED** after 3 legacy repair cycles when sandboxed verification hit `EAI_AGAIN registry.npmjs.org`, leaving **4 open MEDIUM findings**. Mission 2 is its retry, ~45 hours later.

---

## 7. Work Produced

5 commits, 2,723 net insertions across 40 files versus the mission base.

| SHA | Attributed writer (GG provenance) | Content |
|---|---|---|
| `d2565e1` | opencode / implementation | Phase-8b workspace slice: outgoing invoices (draft→finalize→cancel, gapless numbering, PDF), partner master data, portal connector registry with 4 collection methods + provenance, Alembic `0010`, 3 new API routers, 3 new Next.js pages, tRPC contracts, 213 lines of new tests |
| `81b7f9e` | claude / testing | Merge of `feat/phase9-ingestion-security` — decompression-bomb guard fixed (was failing open twice over), clamd AV scanning, streaming body limits, Pillow/pypdf/pytesseract promoted to hard deps |
| `ae5ea6a` | claude / testing | `/health` converted from a page to a route handler that can actually return 503; middleware matcher fixed; tautological test (`expect("/health").toBe("/health")`) replaced with 5 real assertions |
| `b494b6c` | claude / testing | `ROUND_HALF_EVEN` → `ROUND_HALF_UP` on VAT and DATEV amounts (kaufmännische Rundung); 18 new tests |
| `bf39469` | claude / testing | `docs/LAUNCH_READINESS_AUDIT.md` — 297 lines |

Test delta: 264 → 324 Python tests passing; 100 frontend tests. Verified independently just now: **324 passed / 30 skipped in 6.3 s**, **100 frontend passed**, `ruff` clean, `mypy --strict` clean on 96 files.

---

## 8. GG Planning Review

**Score: 4/10.** The planner's *output* was excellent. GG's *planning* was not.

What went right: the compiled planner role produced a genuinely superb audit — it discovered the two feature branches were **diverged, not stacked** (the actual root cause of everything the mission mis-described as "missing"), found that the OCR path could never execute because `pytesseract`/`Pillow`/`pypdf` were undeclared dependencies, and found the decompression-bomb guard failing open. It then verified the signed-in UI over real HTTP.

What went wrong, all of it GG's doing:

1. **GG's role contract contradicted the mission objective, and GG never noticed.** The compiled `role:planner` output contract said *deliver one plan document*. The objective said *"Your job is to finish the existing product, not merely propose a plan"* and *"Do not stop after producing a roadmap."* Claude flagged the conflict explicitly in its own output:

   > "The ROLE AND AUTHORITY and OUTPUT CONTRACT for this invocation designate a product planner whose deliverable is one plan document. The objective text additionally asked for the implementation to be finished. **These conflict; I followed the role/output contract.**"

   GG had a provider telling it, in writing, that its own prompt was self-contradictory. Nothing consumed that signal.

2. **The plan was then thrown away.** `engine.py:664` truncates the previous stage's output to **200 characters** for the handoff. The 43,961-character plan reached the implementer as:

   ```
   - [planning] claude: Audit complete. Per the role and output contract I'm delivering
     the verified plan, not code changes — no files in the repo were modified this pass.

   ```PRODUCT_PLAN_JSON
   {
     "schema_version": "1.0",
   ```

   That is the entire downstream inheritance from a 10-minute Claude run that moved the 5-hour quota window from 0 % to 16 %. The branch-divergence discovery, the gap matrix, the priority order — none of it reached opencode.

3. **No decomposition.** A ~40-deliverable objective was executed as five monolithic stages. No phases, no requirements, no acceptance criteria, no DAG. `plan_revisions`, `project_phases`, `tasks` (DAG sense), `dag_revisions` are all empty.

4. **No scope negotiation.** GG has no mechanism to say "this objective is 30× larger than one mission." It simply ran it and then declared `COMPLETED`.

---

## 9. Context Compiler Review

**Score: 3/10.** Every run compiled cleanly (`capture_status=CAPTURED`, `redaction_status=REDACTED`, zero warnings, well under budget). The *machinery* is sound. What it packed was not.

### Representative manifests

| Run | Role | Prompt chars | Est. tokens | Budget | Blocks |
|---|---|---|---|---|---|
| `run-b9f788e12af4` | planner | 13,016 | 3,254 | 20,000 | sys, objective, output |
| `run-7d1c04e9f0cd` | implementer | 25,916 | 6,479 | 16,000 | sys, objective, output, env |
| `run-7f680deb08c8` / `run-78e5900bdffb` | testing | 25,900 | 6,475 | 12,000 | sys, objective, output, env |
| `run-9197c7cb53bf` / `run-459f988cd722` | reviewer | 26,211 | 6,553 | 18,000 | sys, objective, **git (131 ch)**, output, writers (131 ch), env |

### Three reproduced defects

**(a) The objective is duplicated verbatim.** `engine.py:468` sets `task_objective = f"{mission.title}\n{mission.task}"` while also setting `task_title=mission.title` and `task_description=mission.task`. `context_compiler.py:623` then concatenates all three. Arithmetic confirms it exactly:

```
"OBJECTIVE / CURRENT TASK\n" (25) + title (39) + 1 + task (12,527) + 1
                                  + title (39) + 1 + task (12,527)   = 25,160
manifest original_chars for every non-planner run              = 25,160   ✓
```

Half of every implementer / tester / reviewer prompt in this mission was a byte-identical repeat of the other half. ~3,150 estimated tokens × 5 runs of pure duplication, and it crowds out the budget that real context needed.

**(b) The reviewer received no diff and no file list.** The GIT_DIFF block was **131 characters**, which decodes exactly to:

```
Candidate range: base=d2565e18… candidate=bf39469b…
Files: (see diff)
```

`spec.git_files_changed` and `spec.git_diff_summary` were both empty. Root cause: `engine.py:454` populates `git_files_changed` from `finding_files(db, mission_id)` — *files named by existing open findings* — so on a mission's first review pass it is always empty; and `git_diff_summary` is never populated anywhere in the sequential engine (only `parallel_engine.py:882` and `repair_worker.py:555` set the parallel/repair equivalents).

The actual diff the reviewer was asked to judge: **187,780 characters, 40 files, +2,723 / −193.** It got 131 characters and "inspect locally". Claude, in mission 1, compensated by investigating on its own and produced 5 findings. AGY did not.

**(c) The review range is wrong, and the ledger disagrees with the prompt.** `context_compiler.latest_candidate_shas()` returns the **last** write-capable run's `before..after`, i.e. `d2565e1..bf39469`. `provenance.record_review_attempt()` independently computes `reviewed_base` as the **earliest** `write_provenance.base_sha` in the mission, i.e. `2a20ca71..bf39469`.

Consequence: the reviewer was told to review 4 commits; the evidence ledger records that it reviewed 5. The commit excluded from the prompt but included in the ledger is `d2565e1` — the 2,661-line implementation. **OpenCode's entire contribution was never independently reviewed, and GG's evidence store says it was.**

### What was preserved / excluded

- Mandatory blocks survived everywhere; nothing was compacted; no `warnings_json` entries.
- **Reviewer contamination avoided** — yes, structurally: the reviewer prompt was identical for codex and agy, and carried a WRITER_PROVENANCE block.
- **Requirements/acceptance preserved** — vacuously; none existed.
- **Dependency handoffs** — not exercised (sequential mode).
- `repeated_context_ratio` measured 0.9865 / 1.0 / 0.9764 / 1.0 across consecutive runs and produced **no warning**. GG can see that it is resending near-identical context and does nothing with the observation.

Larger prompts are not the problem here. The problem is that the large prompt was 50 % duplicate boilerplate and 0 % diff.

---

## 10. Provider Usage

| Provider | Calls | Roles | Successes | Failures | Known usage | Comments |
|---|---|---|---|---|---|---|
| claude | 2 | planning, testing | 2 | 0 | **COMPLETE** (`PROVIDER_REPORTED`). Planning: 2,727,941 in / 41,407 out (2,638,509 cache-read). Testing: 8,367,456 in / 62,224 out (8,231,339 cache-read) | Did the substantive engineering. 5-hour quota window 0 %→16 %, then 25 %→62 %; 7-day 40 %→43 % — **captured in the logs, discarded by GG** |
| opencode | 1 | implementation | 1 | 0 | **PARTIAL** (`CLI_REPORTED`, `opencode:step-finish`): 39,396 in / 773 out / 107,309 native — `{"steps": 4, "unfinished": 5}` | Wrote 2,661 lines while emitting nothing to stdout after second 11 |
| codex | 2 | testing, review | 0 | 2 (`RATE_LIMIT`) | **UNKNOWN** (`codex:no-terminal-usage`) ×2 | 407 s of real work (118 command executions, 2 web searches) destroyed; then a 5 s re-attempt 19 min later |
| agy | 1 | review | 1 | 0 | recorded **UNKNOWN** (`agy:telemetry-not-captured`) — **but the log's final `result` event contains `{"input_tokens":416729,"output_tokens":32542,"thinking_tokens":23929,"cache_read_tokens":4885861,"total_tokens":449271}`** | `parse_agy_usage()` is a stub returning `unknown_usage()`; the data was on disk and unparsed |

GG's honesty rule held: `UNKNOWN` was never rendered as zero, and the UI correctly states "Remaining subscription quota is not reported." But see §14 — that statement is now only true because GG chooses not to read what it is given.

---

## 11. Parallelism / Task Decomposition

**N/A for parallelism** — `scheduling_mode = SEQUENTIAL`. Zero rows in `tasks` (DAG), `task_dependencies`, `task_dependency_inputs`, `task_branches`, `task_integrations`, `task_locks`, `dag_revisions`. No worktrees created. Parallel overlap: **0 s of 3,045 s**.

**Decomposition: 3/10.** This is the clearest case in the run where GG had a capability and did not reach for it. The objective contained obviously independent workstreams — outgoing invoicing, partner master data, portal connectors, security hardening, the pilot dataset — with different files and almost no overlap. `opencode` in fact implemented three of them (`outgoing.py`, `partners.py`, `portal.py` + three routers + three pages) in a single undifferentiated 7-minute run. Those were three natural DAG tasks with clean workspace scopes and no conflicts.

GG's Increment 3B machinery — pinned input SHAs, ancestry-validated results, sibling isolation, stale-descendant handling — is built, sealed and tested, and contributed **nothing** to the one real dogfood, because nothing routed the operator toward it. Worse: because implementation was one opaque blob, the review range problem in §9(c) had maximum blast radius. With three tasks there would have been three reviewable artifacts.

---

## 12. Git & Artifact Provenance

**Score: 8/10. This is GG's best subsystem and it earned its keep.**

Verified lineage (all ancestry checks pass):

```
2a20ca71  mission base (= origin/feat/self-serve-signup)
   │
   ├─ wprov claude/planning      base 2a20ca71 → result 2a20ca71  tree 6c0467e9  dirty_before=0
   │
d2565e18  wprov opencode/implementation  base 2a20ca71 → result d2565e18  tree bbe1b0ca
   │      (GG checkpoint, message "agent(opencode): implementation checkpoint")
   │
   ├─ wprov codex/testing        base d2565e18 → result d2565e18  (no change, rate-limited)
   │
81b7f9e ── merge ── b2c3fff (feat/phase9-ingestion-security)
ae5ea6a
b494b6c
bf39469  wprov claude/testing    base d2565e18 → result bf39469b  tree 92b344fe
   │
   ├─ wprov codex/review         base bf39469b → result bf39469b
   └─ wprov agy/review           base bf39469b → result bf39469b
```

What GG got right that a human reading `git log` would get wrong:

- **Every commit is authored `AyhamJo7 <mhd.ayham.joumran@studium.uni-hamburg.de>`.** Author strings are worthless for attribution here. GG's `write_provenance` correctly identifies opencode as the writer of `d2565e1` and claude as the writer of `bf39469b`, from contribution, not from invocation role or commit metadata. This is exactly what the provenance increment was built for and it works.
- **`dirty_before = 0` on every run.** No pre-existing workspace change was ever attributed to a provider. Mission 1, by contrast, correctly recorded `"checkpoint before mission start (pre-existing changes)"` as a distinct, labelled checkpoint.
- **GG refused to certify independence, correctly.** `reviews.independent = 0`, `degradation_reason = "writer provenance incomplete (cannot certify independence)"`. The reason is legitimate: claude produced **four** commits inside one run, and `write_provenance` records only `base → result`, so `81b7f9e`, `ae5ea6a` and `b494b6c` have no provenance row. `range_writers()` found 3 of 5 commits unattributed, set `complete=False`, and fail-closed. AGY genuinely was outside the writer set `{claude, opencode}` — GG *could* have called this independent and did not, because it could not prove it. **That is the guarantee working under real-world conditions.**

Flags: no orphan commits, no dirty workspace, no unknown external writer, no incorrect adoption, no manual edits (every in-window commit falls inside a provider run window), no destructive Git action.

The one integrity defect is §9(c): the ledger's review range (`2a20ca71..bf39469b`) over-credits the reviewer's actual prompt range (`d2565e1..bf39469b`) by one 2,661-line commit.

---

## 13. Independent Review Quality

**Score: 3/10.** Judged on evidence, not on the finding count.

**In-window (mission 2):** one review, by AGY, over 431 s, `num_turns = 1`. Its own transcript:

> "I have started searching for the RechnungsRadar repository and commit references across the system. I will proceed as soon as the search completes. Waiting for search task to finish. I am waiting for the background search tasks to report back. I will pause tool calls and wait for the search task to notify me of completion. …"

and its stderr:

> `root agent idle; waiting for 2 background task(s) (bounded by --print-timeout)`

AGY spent essentially the whole run blocked on its own background subagents, then emitted a single finding at the timeout boundary. The finding itself is **real and actionable** — `/ingest/capture` is missing from `_INGEST_UPLOAD_PATHS` and lacks `Depends(require_upload_rate_limit)`, so capture uploads bypass streaming body limits and per-tenant rate limiting. I confirmed it against the source. One true positive, no false positives.

But: one MEDIUM on a 40-file, 2,723-line change that merged an entire security branch and altered VAT rounding. It did not look at the rounding change. It did not look at the merge. It could not have looked at `d2565e1` at all, because the prompt excluded it.

GG recorded `run_status = SUCCEEDED`. **GG's terminal-outcome classification is exit-code + output-parseability; it has no notion of "the reviewer did not do the work."** The `stderr` line saying the agent was idle was captured and ignored.

**Out-of-window comparison (mission 1, same architecture, reviewer = claude):** 5 findings on the first pass, including one HIGH that was specific, correct and structural — `NormalizedInvoice` carried no VAT breakdown at all (no BT-117/BT-118/BG-23), so the DATEV `tax_key` was being inferred by an LLM over line-item text with no deterministic ground truth. That is a genuinely excellent finding. Two HIGHs were later verified fixed **with explicit file:line evidence** and correct lineage. Re-flagged findings carried `"Unchanged since the prior review pass"` and quoted line numbers.

So the review *mechanism* — fingerprinting, re-flag-or-verify contract, `repair_attempted` vs `open` vs `resolved`, refusal to treat omission as resolution — is well designed and demonstrably works. What failed in this dogfood is that the mechanism's quality is entirely hostage to (a) which provider draws the role and (b) how much diff context the compiler gives it, and GG currently controls neither.

---

## 14. Verification / Acceptance

**Score: 6/10.**

What ran, at `bf39469b`, `kind=toolchain`, 10.3 s total, all PASS:

```
uv run pytest -q        exit 0  5.3 s
pnpm run test           exit 0  1.6 s
uv run ruff check .     exit 0  0.1 s
pnpm run lint           exit 0  2.3 s
uv run mypy .           exit 0  1.3 s
pnpm run typecheck      exit 0  1.6 s
```

I re-ran all of it independently. It is **real**: 324 Python tests + 100 frontend tests actually execute and pass; `mypy --strict` is clean on 96 files. This is not a weak gate, and the speed is genuine, not a no-op.

Three real weaknesses:

1. **Exit code is the only oracle.** `pytest` reported `324 passed, 30 skipped`. GG recorded `[PASS]`. The 30 skips include the **5 Postgres RLS tenant-isolation tests** and the **5 orchestrator end-to-end graph tests** — i.e. the product's single most important safety property is unverified, and GG's verification record does not say so. RechnungsRadar even ships `RR_REQUIRE_SECURITY_TESTS=1` to convert those skips into failures; GG has no way to know that and no skip accounting of its own. Claude's own audit was more honest than GG's ledger here: *"The 30 skips are not green."*
2. **No build step.** The detected toolchain has `build_commands: none detected`; `pnpm build` never ran under GG. Codex (mission 2) and the audit both ran it manually and it passes — but GG's gate would not have caught a broken production build.
3. **No fresh-checkout verification for missions.** `fresh_checkout_attempts` and `criterion_attempts` are both empty. That capability is bound to the product-acceptance path, which this workflow never enters. Mission 1's failure was *precisely* an environment/dependency issue that fresh-checkout verification exists to surface — and the mission workflow cannot reach it.

Credit where due: mission 1 shows the gate **can** fail honestly, and shows GG classifying correctly. When `pnpm` needed the network inside the sandbox and got `EAI_AGAIN registry.npmjs.org`, GG routed it to `UNVERIFIED` with the persisted reason *"verification could not run due to an environment/tooling issue, not a code defect — no repair attempt was made"* rather than burning repair cycles on an unfixable DNS lookup. That is a good, deliberate behaviour and it worked in the wild.

---

## 15. Autonomous Repair

**Increment 4 bounded autonomous repair: NOT EXERCISED.** `repair_cycles` = 0 rows, `repair_attempts` = 0 rows, `project_gates` = 0. The canary result you already have (OpenCode repair, Codex review, automatic scheduler, exact-SHA recheck) remains the only evidence; **this dogfood adds nothing to it**.

**Legacy mission review/repair loop: not triggered in-window.** `open_blockers()` filters `severity IN ('BLOCKER','HIGH')`. AGY's single finding was MEDIUM, so `blockers` was empty and the loop returned success immediately. That is correct per design — but it means the mission reached `COMPLETED` with an open MEDIUM security finding and no gate, no prompt, and no distinction from a clean pass.

For contrast, mission 1 (out of window) exercised the legacy loop three times: claude review → opencode repair → claude re-review → opencode repair → claude re-review → opencode repair. Two HIGH findings were genuinely closed with verified evidence and correct lineage. It was bounded correctly (`max_repair_cycles=3`) and stopped. Notably, it did **not** weaken tests or oracles: the repairs added `packages/rr-invoice/rr_invoice/vat.py`, `test_classifier_vat.py` and `test_vat.py`. That loop works.

---

## 16. Human Gates / Operator Interventions

`human_gates` = 0 rows. `orchestration_operations` = 0 rows. No `MISSION_PAUSED`, no resume, no cancel, no adoption, no manual retry, no manual Git action, no manual code edit (verified: every in-window commit falls inside a provider run window, and the tree was clean at every boundary).

**Exactly one observable operator intervention in 24 hours:**

| Time | Intervention | Expected? | Could GG have handled it? | Would more autonomy have helped? |
|---|---|---|---|---|
| 02:14:47 | Created mission `8344d7d9` as a **Retry** of the UNVERIFIED mission `44cdae6d`, ~45 h after it stopped | Yes — GG correctly stopped rather than looping on an unfixable environment failure | Partly. The blocker was "Docker/network unavailable in the verification sandbox". GG could reasonably have offered a one-click retry from the mission's blocking-issue card, or a "retry when toolchain becomes available" option. It cannot fix the environment itself, and should not. | **No.** This is a case where GG stopping was right. The 45-hour gap is an operator-attention cost, not a GG failure. |

That is a genuinely strong result: **51 minutes of fully unattended work with zero interruptions.** The problem is not that GG asked too much of you. It is that GG asked *nothing* of you at the two moments where it should have — when the reviewer plainly did no work, and when the mission completed with an open MEDIUM security finding and a review it had itself refused to certify.

---

## 17. Time Efficiency

Directly measured from `provider_runs` and `missions`:

| Metric | Value |
|---|---|
| Mission wall clock | 02:14:47 → 03:05:31 = **3,045 s (50.7 min)** |
| Total provider execution | **3,033 s (99.6 % of wall clock)** |
| Orchestrator overhead | **11 s** |
| Deterministic verification | 10.3 s |
| Parallel overlap | 0 s (sequential by configuration) |
| Time waiting on operator | 0 s in-window |
| `WAITING_FOR_PROVIDER` / cooldown waits | 0 s |
| Review time | 436 s (14.3 %) |
| Repair time | 0 s |
| Planning time | 598 s (19.6 %) |
| Implementation time | 446 s (14.6 %) |
| Testing time | 1,554 s (51.0 %), of which 407 s discarded |

**Orchestration overhead of 11 seconds on a 51-minute mission is excellent.** GG is not slow. Whatever it wastes, it does not waste on itself.

> **Did GG save time versus manually coordinating the same providers?**

Marginally, and not for the reason you would hope. It saved the ~4 context-switches you would have made across four terminals, and it ran unattended at 02:14. But it did not save provider time — it *spent* provider time you would not have spent (a planning run whose output it discarded, a codex run it re-attempted into a known-exhausted quota), and it did not save wall clock, because it ran everything sequentially when the work was parallelisable. A human coordinating the same four CLIs would have (a) skipped the planning stage or actually pasted the plan forward, (b) not called codex again 19 minutes after it said "try again at 6:37 AM", and (c) noticed within 10 seconds that the reviewer was idle-waiting.

Net: **roughly a wash on time, a clear win on unattendedness, a clear loss on quota.**

---

## 18. Wasted Work

Quantified from timestamps and artifacts:

| Waste | Evidence | Cost |
|---|---|---|
| Codex testing run destroyed by rate limit | `run-7f680deb08c8`: 407 s, 118 command executions, 2 web searches; `tree_sha` unchanged, 0 commits; its findings ("production frontend build passes"; "Python suite stalls in the synchronous FastAPI TestClient"; "the required security gate fails") appear nowhere downstream | **407 s + one codex quota block, total loss** |
| Failover discarded the failed run's partial findings | `run-7f680deb08c8` and `run-78e5900bdffb` have the **identical** `prompt_hash 07b10893…`. The retry carried no `FAILURE_EVIDENCE` block, so claude re-derived from scratch what codex had already established | Duplicated ~7 min of investigation |
| Codex re-selected 19 min after declaring exhaustion | `run-9197c7cb53bf`, 5 s, `RATE_LIMIT`. `providers.cooldown_until` for codex is **NULL** — the sequential engine never sets a cooldown on RATE_LIMIT, and never parses the reset time the provider itself printed ("try again at 6:37 AM") | 5 s + one wasted invocation; avoidable with data already in hand |
| Planning output truncated to 200 chars | `engine.py:664`; 43,961-char plan → 200-char fragment ending mid-JSON | **598 s + 2.73 M input tokens + 16 pp of the 5-hour Claude window, ~total loss** |
| Objective duplicated in every non-planner prompt | 12,566 redundant chars × 5 runs ≈ 15,700 estimated tokens | Budget crowd-out |
| OpenCode's implementation never reviewed | Review range excluded `d2565e1` (§9c) | 2,661 lines of unreviewed code delivered as `COMPLETED` |
| Three mission-1 findings silently dropped on retry | §21 | Three real defects still live at HEAD |

**Provider wall-clock that produced no durable downstream contribution: 1,010 s of 3,033 s ≈ 33 %.**

---

## 19. Operator UX — first real measurement of Operator Experience v1

**Score: 4/10.** This is the section you asked for, and the fixture-based validation did not catch any of it.

### The Overview showed nothing about this run

`OverviewPage.tsx` is built entirely around **product lifecycles**:

- "Needs your attention" → `products.filter(productAttention)`, plus a collapsed `<details>` for missions in `["WAITING_FOR_HUMAN","BLOCKED","FAILED","UNVERIFIED"]`.
- "In progress & ready to start" → active products, plus missions **excluding** `COMPLETED`.
- "Recent outcomes" → `products.filter(FINISHED_PRODUCT_STATES)` — **products only**.

RechnungsRadar was run through **Repositories → New Mission**. `product_projects` is empty. So after the dogfood finished at 03:05:31:

- Mission 2 (`COMPLETED`) appears in **none** of the three Overview sections.
- Mission 1 (`UNVERIFIED`) appears only inside a collapsed disclosure labelled "1 mission stops".
- The repair-cycle fan-out (`REPAIR_DETAIL_LIMIT`, the `productAttention` helper, the whole repair polling path) was entirely inert.

**The new Overview was optimised for the workflow you did not use.** For this run its value was approximately zero — the operator's only real surfaces were the Missions list and Mission Control. That is the single most important UX finding from this dogfood, and it is invisible to fixture testing because the fixtures supply products.

### The degraded-review card would have shown a false statement

`api/app.py:271` sets `degraded_review = (latest_review.independent == 0)`. For this mission that is **true**, so `MissionControlPage.tsx:160` would render:

```
[SELF-REVIEW]  DEGRADED REVIEW — independent reviewer unavailable
Reviewer agy also performed the implementation. writer provenance incomplete
(cannot certify independence)
```

**"Reviewer agy also performed the implementation" is false.** The writer set was `{claude, opencode}`; AGY wrote nothing. The card hardcodes the self-review explanation for every `independent=0` cause. The true reason is appended afterwards, so the operator sees a confident falsehood followed by the correct fact. This is a direct violation of the truthfulness rule — the UI fabricated a cause it did not have evidence for.

### `COMPLETED` is not distinguishable from clean

At 03:05:31 the mission became `COMPLETED` with:

- one **open MEDIUM security finding** (unrate-limited upload endpoint),
- a review GG had itself **refused to certify as independent**,
- a reviewer that spent its run idle,
- 2,661 lines never covered by the reviewer's prompt,
- 30 skipped security tests counted as PASS.

Nothing in the mission verdict, the Overview, or the badge conveys any of that. `COMPLETED` renders the same as a spotless run.

### What worked

- `RunInspector` / `UsageValue` never render unknown as zero — verified in source, and `run_usage` has three genuine `UNKNOWN` rows to prove it.
- "Availability is observed locally. Remaining subscription quota is not reported." is honest and correctly scoped.
- The `ReviewFindingsPanel` and `GitPanel` would have shown the finding and the SHA ledger correctly.
- Logs are correctly secondary (`<details><summary>Live provider output & logs</summary>`). Right call — nothing in this run needed raw logs *for normal operation*… which is itself telling, because everything I needed for *this review* was only in the raw logs.

### Did the operator need terminal/SQLite/source inspection?

**Yes — comprehensively.** Every significant finding in this report came from SQLite or from `.orchestrator/logs/*.stdout.log`, not from the UI: the 131-char reviewer context, the 200-char handoff truncation, AGY idling, the objective duplication, Claude's quota utilisation, the review-range mismatch, the dropped findings. The UI is good at showing *what state things are in*. It has no surface at all for *what the providers were actually told*, even though `/api/runs/{id}/context` exists and the manifests are complete and well-formed. The Run Inspector shows the manifest; nothing draws attention to a 131-character mandatory block.

---

## 20. RechnungsRadar Code / Product Quality

**Score: 7/10 — independent of how it was produced.**

**Architecture.** Clean monorepo: `packages/{db,python-common,rr-invoice,rr-erp}` + `services/{ingest-api,orchestrator,imap-fetcher}` + `web` (Next.js 15) + `jvm` sidecars (KoSIT validator, parser, AS4 gateway). The separation the mission asked for — deterministic structured-invoice path vs. best-effort extraction path — is real and enforced in code, not just documented. Connectors sit behind one `Connector` protocol. `NormalizedInvoice` is EN 16931 BT-coded with a proper `VatBreakdown` (BT-116/117/118/119, BG-23), added by mission 1's repair cycle in response to a review finding.

**Security.** Postgres FORCE-RLS with a restricted app role and a separate admin role for migrations; `tenant_connection()` opens an explicit transaction and sets `app.tenant_id` as a local setting — correct. Hardened `lxml` parsing forbidding DTD/DOCTYPE. WORM write before processing. SHA-256 dedup. Content-type-vs-byte-sniff mismatch → 415. Decompression-bomb guard now keyed on the independent sniff rather than the client's declared type (this run's fix), with Pillow promoted from an optional import that silently no-op'd to a hard dependency. `clamd` scanning after the WORM write, failing closed. Secrets are env/Secrets-Manager based; `.env.example` only; a dedicated `test_secret_scrub.py`.

**Correctness.** The rounding fix is the standout: `Decimal.quantize()` defaults to `ROUND_HALF_EVEN`, which resolves a half-cent to the even neighbour. For German commercial practice that is wrong, and it was under-reporting VAT on 3 of 6 representative restaurant line items (1.50 € @ 19 % → 0.28 instead of 0.29) and in DATEV export. Both paths now use `ROUND_HALF_UP`, with 18 tests. The audit correctly flags that this *changes emitted figures* and needs Steuerberater confirmation. I verified `next_outgoing_number()` — its check-then-increment runs inside the `tenant_connection()` transaction with an `ON CONFLICT DO UPDATE … RETURNING` row lock, so gapless numbering is genuinely serialised. That one is correct.

**Types / lint.** `mypy --strict` clean over 96 files, `ruff` clean, `tsc --noEmit` clean, `eslint` clean. **Zero TODO/FIXME/HACK/XXX** across `packages`, `services`, `web`. That is unusually disciplined.

**Tests.** 324 Python + 100 frontend. The health-probe fix is instructive: the old test was `expect("/health").toBe("/health")` — a tautology that could never fail — replaced with 5 real assertions plus a live 503/200 check. Someone was actually reading the tests.

**Weaknesses.**

- **No e2e/browser automation anywhere** (no Playwright, no Cypress). The mission explicitly asked for it. "Browser testing" was done by driving HTTP by hand and recording it in prose.
- **30 skipped tests include DB-level RLS.** Multi-tenant isolation — the product's core safety property — has never executed in this environment. The audit says so plainly; GG's ledger does not.
- **Quarantined documents are invisible to tenants** (`status='quarantined'` filtered out of both inbox tabs). A malware hit silently swallows an invoice. Correctly flagged as a pilot blocker by the audit; **not found by GG's reviewer**.
- Three defects GG found two days ago are still live — §21.
- `docs/LAUNCH_READINESS_AUDIT.md` commits `AUTH_SECRET=dev-secret AUTH_DEMO_PASSWORD=demo1234` as copy-paste run instructions. They are demo values and the same doc warns never to ship them, but committing runnable credentials into a repo doc is a habit worth not forming.

**The audit document itself is the best artifact of the run.** It separates engineering completion from external blockers, refuses "all green" (explicitly: *"The 30 skips are not green"*), names 8 concrete external actions, and gives a split verdict — *go for local demo, no-go for pilot, no-go for sales*. It also states plainly, unprompted: *"the objective asked for a far larger build than was completed."* That is exactly the honesty the mission demanded, and it came from the provider, not from GG.

---

## 21. Requirement Coverage

Against the objective's nine numbered demands. "Evidence" is what exists at `bf39469b`; "GG criterion" is what GG's own machinery proved.

| # | Requirement | Implementation evidence | GG criterion / review | Status |
|---|---|---|---|---|
| 1 | Audit before changing; gap matrix; then execute | 43,961-char planner audit + 297-line `LAUNCH_READINESS_AUDIT.md` with feature matrix | None (no criteria exist); plan discarded at handoff | **Fully demonstrated** (by provider, not by GG) |
| 2 | Unified invoice-management workspace | Outgoing invoices, partners, portals, manual-entry lane, dedup, exception inbox, archive | toolchain PASS only | **Weakly demonstrated** — bulk import, drag-and-drop, notifications, search still missing |
| 3 | Supplier-portal aggregation framework | `portal.py` registry, 4 collection methods, status/last-sync/error/required-action, per-document origin | not reviewed (outside review range) | **Weakly demonstrated** — honest scaffolding; only `assisted_upload` operational, no browser extension |
| 4 | Deterministic finance separate from AI; per-field source/confidence | `extraction.py` returns value/confidence/source/needs_review; `vat.py` constrains `tax_key` against BG-23; `ROUND_HALF_UP` fix | 18 rounding tests + `test_vat.py` | **Fully demonstrated** |
| 5 | End-to-end business workflow, polished German UI | 7 signed-in pages render; Auth.js flow; German throughout | HTTP-level checks in prose; no e2e suite | **Weakly demonstrated** |
| 6 | Integrations + commercial readiness | DATEV/Lexware/SAP B1 behind one protocol; Stripe checkout/webhook/allowance in TEST mode; Peppol receive-only | live connector tests all skip | **Blocked externally** — needs UG registration, live Stripe, connector credentials |
| 7 | Security / tenant isolation / GoBD-GDPR | FORCE-RLS, bomb guard, AV scan, body limits, WORM, hash-chained audit, GoBD export | **RLS tests skipped**; one MEDIUM finding open; 3 older findings lost | **Weakly demonstrated** — the core property is unverified |
| 8 | Separate review pass; unit/integration/e2e/regression; run the app in a browser | 424 tests; manual HTTP walkthrough | **one MEDIUM finding on a 40-file diff**; no e2e framework | **Weakly demonstrated** |
| 9 | Pilot dataset + checklist for the restaurant UG | `scripts/pilot-demo-seed.py`, `docs/PILOT_CHECKLIST.md` | none | **Weakly demonstrated** |

### Findings GG found and then lost

Mission 1 ended with four open MEDIUM findings. Mission 2 is its retry with a new `mission_id`. Both `_prior_findings_context()` and `open_findings_for_scope()` filter on `mission_id`, and `retry_of_mission_id` is used only for linking — **never to seed the prior ledger**. All four dropped out of GG's tracking. I checked each against `bf39469b`:

| Finding | Status at HEAD |
|---|---|
| `web/src/server/oauth-email.ts` — `stateSecret()` falls back to the literal `"dev-state-secret"` when `AUTH_SECRET` and `INTERNAL_INGEST_TOKEN` are both unset (forgeable OAuth state HMAC → mailbox binding to an arbitrary tenant) | **STILL PRESENT** (`oauth-email.ts:54-56`) — raised 3× by Claude, never fixed |
| `web/app/api/sign-up/route.ts` — public unauthenticated signup with no rate limiting and a distinguishing `409` for existing emails (account enumeration) | **STILL PRESENT** (`route.ts:41-44`) |
| `packages/rr-invoice/rr_invoice/persistence.py` — `has_document_allowance` (SELECT) and `increment_document_usage` (UPDATE) in **separate** `tenant_connection()` transactions with a WORM write between them; concurrent bulk upload oversubscribes a tenant's paid allowance | **STILL PRESENT** (`persistence.py:421-436`) |
| `ManualEntryForm.tsx` — no VAT rate/amount input, so the new deterministic VAT constraint is unreachable for exactly the invoice class with no other ground truth | **STILL PRESENT** |

Three of those are security or financial-correctness defects that **GG itself discovered, recorded, fingerprinted, and then silently forgot** — and the mission that followed was marked `COMPLETED`. This is the most serious process failure in the dogfood.

---

## 22. Safety / Security

Checked against `SECURITY.md`'s stated boundaries. **No violations found.**

| Check | Result |
|---|---|
| Secrets committed | None. `.env.example` only. No key-shaped strings (`sk-`, `ghp_`, `AKIA`) anywhere in the 22 provider logs. |
| Secrets in logs | None detected. Note `REDACTED` count is 0 — nothing needed redacting, so the heuristic was not actually exercised here. |
| Runtime evidence committed | No. `.orchestrator/` is gitignored (`.gitignore:60`, verified with `git check-ignore`). |
| Unsafe shell | Providers ran with substantial local authority by design (`--permission-mode bypassPermissions`, `--dangerously-skip-permissions`, `--sandbox workspace-write`, `--auto`), which is documented in `SECURITY.md` and `README.md` as the accepted boundary. |
| Broad filesystem edits | None. All writes inside the registered workspace. Notably AGY searched "across the system" per its own transcript — allowed by its flags, and it found nothing outside the repo, but worth watching. |
| Network behaviour | Codex made 2 web searches during testing. Expected for the role; no exfiltration indicators. |
| Permission escalation | None. Docker was correctly left alone because starting it needs sudo — the provider said so and stopped. |
| Destructive Git | None. No force-push, no reset, no branch deletion. Nothing pushed to `origin` (still at the July `80be05f`). |
| Provider scope | Correct `cwd` on every run; workspace root inside `~/projects` per `security.allowed_roots`. |

One observation, not a violation: the run created a new branch (`integrate/phase9-into-self-serve`) and merged another branch into the mission line. GG recorded the resulting SHAs correctly but has **no model of branch topology** — it tracks SHAs, not the branch the mission is on. That worked out here; it is a latent surprise.

---

## 23. What GG Did Best

Ranked, each backed by evidence in this run.

1. **Refused to certify a review it could not prove.** `independent=0`, `"writer provenance incomplete (cannot certify independence)"`. AGY genuinely was outside the writer set; GG could have called it independent and chose fail-closed because 3 of 5 commits in the range had no provenance row. This is the hardest thing to get right and GG got it right unprompted, in the wild.
2. **Correct writer attribution against worthless Git metadata.** All five commits are authored `AyhamJo7`. GG identified opencode as the writer of `d2565e1` and claude as the writer of `bf39469b` from actual committed contribution. Without this, the entire artifact ledger would have been fiction.
3. **Made an opaque provider auditable.** OpenCode emitted 19 stream events in the first 11 seconds and then went dark for 7 minutes while writing 2,661 lines across 34 files. GG has almost no telemetry for that run — but it has the exact base SHA, result SHA, tree SHA, `dirty_before=0`, and a labelled checkpoint commit. **The provenance layer recovered what the telemetry layer lost.** That is the increment paying for itself.
4. **Honest UNKNOWN.** Three runs have genuinely unknown usage and GG says so, with a reason code per provider (`codex:no-terminal-usage`, `agy:telemetry-not-captured`). Zero fabrication, zero unknown-as-zero.
5. **Correct environment-vs-code classification (mission 1).** `EAI_AGAIN registry.npmjs.org` inside a network-less sandbox routed straight to a labelled `UNVERIFIED` — *"no repair attempt was made"* — instead of spending repair cycles on a DNS lookup no code change can fix. Exactly right.
6. **Failover worked, twice, cleanly.** Both codex failures were classified `RATE_LIMIT`, both produced a `PROVIDER_RATE_LIMITED` event, both immediately re-dispatched to an eligible alternative, both with a deterministic identical compiled prompt. No stuck mission, no operator intervention.
7. **11 seconds of orchestrator overhead on a 51-minute mission**, and 51 minutes fully unattended.
8. **Deterministic verification that is real.** 424 tests, `mypy --strict`, two lint suites, two typecheckers, in 10.3 s at the exact delivered SHA — and mission 1 proved the gate can fail.
9. **Mission 1's review→repair loop genuinely worked.** Two HIGH findings closed with file:line verification evidence; re-flagged findings carried `"Unchanged since the prior review pass"`; repairs *added* tests (`vat.py`, `test_vat.py`, `test_classifier_vat.py`) rather than weakening oracles.

---

## 24. What GG Did Poorly

Ranked by demonstrated harm.

1. **Lost its own findings on retry.** Four MEDIUM findings from mission 1, never carried forward; three are still live defects at HEAD, including a hardcoded HMAC secret fallback and an unauthenticated, unthrottled signup endpoint. GG found them, fingerprinted them, and forgot them — then said `COMPLETED`.
2. **Gave the reviewer 131 characters of context for a 187,780-character diff.** No file list, no diff summary. The root cause is `engine.py:454` sourcing `git_files_changed` from *existing findings* rather than from Git, which is empty by construction on a first review.
3. **Reviewed the wrong range and recorded a wider one.** The reviewer prompt covered `d2565e1..bf39469`; the evidence ledger records `2a20ca71..bf39469`. The 2,661-line implementation commit was never reviewed, and GG's own trust surface says otherwise.
4. **Discarded the planning run.** 598 s, 2.73 M input tokens, 16 pp of the Claude 5-hour window → a 200-character fragment ending mid-JSON. `engine.py:664`.
5. **Accepted a reviewer that did no work.** AGY, `num_turns=1`, idle-waiting per its own stderr, one MEDIUM finding. `run_status=SUCCEEDED`. GG's outcome classification cannot tell effort from exit code.
6. **No decomposition of an obviously decomposable objective.** Three independent workstreams ran as one opaque blob; the entire Increment 3B DAG apparatus sat unused.
7. **Duplicated the objective verbatim in every non-planner prompt.** 12,566 redundant characters × 5 runs, arithmetically confirmed against the manifests.
8. **Re-selected an exhausted provider.** Codex printed its own reset time; GG set no `cooldown_until` and called it again 19 minutes later.
9. **Threw away a failed run's partial findings on failover.** Identical `prompt_hash` between `run-7f680deb08c8` and `run-78e5900bdffb` — no `FAILURE_EVIDENCE` block, so claude re-derived what codex had already found.
10. **Counted 30 skipped tests — including all DB-level tenant-isolation tests — as PASS.**
11. **The Overview showed nothing about the run**, and the degraded-review card would have stated a falsehood about who wrote the code.

---

## 25. Scorecard

| Dimension | Score | Rationale (required for < 7) |
|---|---|---|
| Planning quality | **4** | The planner's artifact was excellent; GG's planning was not. Role contract contradicted the objective (the provider said so in writing and nothing consumed it); output truncated to 200 chars; no requirements, criteria or phases produced. |
| Task decomposition | **3** | None attempted. A ~40-deliverable objective ran as 5 monolithic stages. Three genuinely independent workstreams were implemented in one opaque commit. |
| Provider orchestration | **6** | Role priorities sensible; failover correct and fast twice. But no cooldown on RATE_LIMIT, the provider's own stated reset time ignored, and a known-exhausted provider re-selected 19 min later. |
| Parallelism | **N/A** | `SEQUENTIAL` mode; zero DAG rows, zero worktrees, 0 s overlap. |
| Context quality | **3** | Objective duplicated verbatim (50 % of every non-planner prompt); reviewer GIT_DIFF block 131 chars for a 187 KB diff; handoff carries 200 chars of the prior stage; no prior-findings carryover across retry; `repeated_context_ratio` ≈ 1.0 with no warning raised. |
| Review quality | **3** | One MEDIUM on a 40-file / +2,723 change, from a provider idle-waiting for most of its run, over a range that excluded the largest commit — recorded as `SUCCEEDED`. |
| Verification quality | **6** | Genuinely real (424 tests, strict mypy, 2 linters, 2 typecheckers, 10.3 s at the exact SHA) and demonstrably able to fail. But exit-code-only: 30 skipped tests incl. all RLS isolation tests scored PASS; no build command; no fresh-checkout path reachable from a mission. |
| Repair quality | **N/A** | Increment 4 not exercised (0 cycles). Legacy loop correctly not triggered — MEDIUM is not a blocker by design. |
| Quota efficiency | **3** | ~33 % of provider wall clock produced nothing durable. Claude 5-hour window 0→16 % then 25→62 %; codex driven to exhaustion and then called again; AGY's full usage record sat unparsed in the log. |
| Git / artifact integrity | **8** | Best subsystem. Correct attribution against identical commit authors, honest refusal to certify independence, clean checkpoint lineage, no orphans/dirt/manual edits. Docked for the review-range mismatch (§9c). |
| Recovery behaviour | **N/A** | No restart, no stale-claim recovery, no worktree reuse. Not exercised. |
| Operator UX | **4** | The new Overview surfaced nothing for this run (product-centric; this was a repository mission). Degraded-review card would state a falsehood about the writer. `COMPLETED` indistinguishable from clean despite an open MEDIUM, a non-certified review, and 2,661 unreviewed lines. Every real finding required SQLite/log inspection. |
| Transparency / trust | **7** | Honest UNKNOWN usage with per-provider reason codes; honest independence degradation; honest environment-vs-code classification; complete context manifests. Held back by the false self-review sentence and by `SUCCEEDED` for an idle reviewer. |
| Final result quality | **7** | Real, verified improvements: a genuine security fix, a genuine VAT-rounding correctness fix, a health probe that can fail, 2,661 lines of coherent new product surface, and an unusually honest audit. Short of 9 because e2e is absent, RLS unverified, and three known defects are still live. |
| **Overall orchestration quality** | **4** | The evidence layer worked; the context layer leaked most of the value; three of GG's own findings were lost; the review stage was hollow. |

---

## 26. Did GG Beat Manual Coordination?

```
ROUGHLY EQUAL
```

Splitting it honestly, because the answer is different on each axis:

**Where GG clearly won:**
- It ran for 51 minutes at 02:14 with zero operator input. You were not in four terminals.
- It produced an artifact ledger a human coordinating by hand would never have built: exact base/result/tree SHAs per run, contribution-based writer attribution against identical Git authors, and a recorded, fail-closed refusal to certify a review it could not prove. **You could not reconstruct that by hand afterwards, and I could not have written §12 of this report without it.**
- It applied a uniform, reproducible verification gate at the exact delivered SHA.

**Where GG clearly lost:**
- You would have pasted the plan forward. GG truncated it to 200 characters and burned a 10-minute Claude run for nothing.
- You would not have called codex again 19 minutes after it printed "try again at 6:37 AM."
- You would have noticed within seconds that the reviewer was idle-waiting, and re-run it.
- You would have pasted the diff, or at least the file list, into the reviewer.
- You would have remembered the `dev-state-secret` finding from two days ago, because you would have had it in front of you.
- You would probably have run the three independent workstreams in parallel.

Roughly a third of the provider budget went to things a person coordinating manually would not have spent. Against that, GG bought unattendedness and auditability. That nets out to a wash *today*.

It is worth being precise about the trajectory: **every loss above is a context-plumbing defect, not an architecture problem.** The provenance and verification layers — the expensive, hard parts — already work. Fix the five items in §29 and this becomes a clear win, because nothing in the win column would be given up.

---

## 27. Would You Use GG?

```
YES, WITH SUPERVISION
```

Based on RechnungsRadar and not on the architecture documents.

I would use it for unattended multi-provider passes on a repository I already trust, overnight, with a clear objective — which is exactly what it did here, and the result was genuinely worth having.

I would supervise three things specifically:

1. **Treat the review stage as advisory, not as a gate,** until the reviewer gets a real diff. A 131-character GIT_DIFF block means the review's quality is whatever the drawn provider feels like doing.
2. **Read the handoff before trusting the chain.** Right now stage *n+1* inherits 200 characters from stage *n*. If the plan matters, it must be in the mission text, not in the planner's output.
3. **Check open findings across missions myself,** because a retry starts the ledger from scratch.

I would not yet use it for work where "COMPLETED" needs to mean something to anyone but me, because `COMPLETED` currently renders identically whether the review was thorough or hollow.

---

## 28. Most Important Dogfood Learning

> **GG's evidence layer survived contact with reality; its context layer did not. GG is currently an excellent bookkeeper of work it briefs badly.**

The hard, expensive guarantees held under genuinely adversarial conditions — identical Git authors on every commit, a provider that went dark mid-run, a cross-branch merge nobody planned, two quota exhaustions, and a reviewer that idled. Provenance, independence certification, honest UNKNOWN, and environment-vs-code classification all did the right thing without being asked.

And yet the run's quality was decided almost entirely by three small plumbing constants that nobody has ever looked at: a `[:200]` in `handoff.py`'s caller, a `git_files_changed` sourced from findings instead of Git, and one duplicated field in a dataclass. Those three lines threw away a 44,000-character plan, hollowed out the review, and doubled every prompt.

The corollary matters more than the finding: **this was not visible from the architecture, from the tests, or from the fixture-based UX validation. It took one real repository and one honest look at the compiled prompts.** The seals were not wrong. They were just guarding the part that was already working.

---

## 29. Top 5 Improvements

Ranked by demonstrated harm. All are small; none is an increment.

| # | Problem | Evidence from RechnungsRadar | Severity / value | Smallest sensible change | Backend change? |
|---|---|---|---|---|---|
| 1 | **Retry missions start with an empty finding ledger** | Mission 2 is `retry_of` mission 1. Four MEDIUM findings dropped; three are still live at `bf39469b`, incl. a hardcoded `"dev-state-secret"` HMAC fallback and an unthrottled public signup endpoint. Mission then reported `COMPLETED`. | **Critical.** GG un-finding its own findings destroys the value of having a review ledger at all. | In `_prior_findings_context()` and `open_findings_for_scope()`, follow the `retry_of_mission_id` chain (it is already stored) and include ancestor findings still `open`/`repair_attempted`. ~1 query. | YES (small) |
| 2 | **Reviewer gets no diff and no file list** | `GIT_DIFF` block = **131 chars** for a **187,780-char / 40-file** diff. `git_files_changed` comes from `finding_files()` — empty on first review; `git_diff_summary` never set in `engine.py`. | **High.** Directly explains a 1-finding review of a 2,723-line change. | In `engine.py`, populate `git_files_changed` from `git diff --name-only base..candidate` and `git_diff_summary` from `--stat` (the compiler already caps at 2,000 chars and 30 files). | YES (small) |
| 3 | **Handoff truncates the prior stage to 200 characters** | `engine.py:664` `result.summary[:200]`. A 43,961-char audited plan reached opencode as a fragment ending `{ "schema_version": "1.0",`. | **High.** Makes the planning stage a pure quota expense. | Raise the cap for the immediately-preceding stage and add a `PHASE_CONTEXT`/plan block to the compiled implementer spec (the compiler already budget-manages blocks; the implementer run used 6,449 of 16,000 tokens). | YES (small) |
| 4 | **Review range mismatch between prompt and ledger** | Compiler: `latest_candidate_shas()` → last run's range `d2565e1..bf39469`. Provenance: earliest `write_provenance.base_sha` → `2a20ca71..bf39469`. Result: `d2565e1` (2,661 lines) unreviewed but ledger-credited. | **High.** A trust surface asserting coverage it does not have. | Make both use one helper. Either extend the reviewer's range to the mission base, or record the narrower range — but they must be the same value. | YES (small) |
| 5 | **Objective duplicated verbatim in every non-planner prompt** | `engine.py:468` passes `mission.task` as both `task_description` and inside `task_objective`; `context_compiler.py:623` concatenates all three. 12,527+39 chars duplicated × 5 runs (arithmetic matches the manifests exactly: 25,160). | **Medium.** Pure waste plus budget crowd-out — it is what leaves no room for a real diff. | Set `task_objective=mission.task` and drop the redundant title/description, or de-duplicate in the compiler. One line. | YES (trivial) |

Worth doing, below the cut: parse AGY's `result.usage` (the data is already in the log, `parse_agy_usage()` is a stub); set `cooldown_until` from a provider's own stated reset time on `RATE_LIMIT`; fix the degraded-review card so it states the actual `degradation_reason` instead of hardcoding "also performed the implementation"; carry a `FAILURE_EVIDENCE` block into a failover retry so 407 s of codex work is not re-derived; surface repository missions on the Overview.

---

## 30. Should GG Change Now?

```
YES — FIX A REPRODUCED BUG
```

Specifically items 1–5 of §29 and nothing else. Justification:

- Each is **reproduced with exact byte counts and line references** from a real run, not inferred from design review. Item 5's arithmetic matches the stored manifest to the character.
- Each is a **small, local change** — a query, a git call, a constant, a shared helper, a dataclass field. None touches a state machine, a migration, or a sealed invariant.
- Item 1 is the one I would not defer. GG currently discards its own security findings on retry and then reports success. That is a correctness defect in the evidence system, and it is live.
- Explicitly **not** justified: another architecture increment, a routing overhaul, a token-learning layer, or an Operator Experience v2. The Overview gap (§19) is real but is a *dogfood limitation first* — one repository mission is not enough evidence to redesign a product-centric dashboard. Run two or three more missions, then decide.

Classification of every weakness found, per your taxonomy:

| Weakness | Class |
|---|---|
| Retry drops prior findings | **BUG** |
| Reviewer receives no diff / file list | **BUG** |
| Handoff 200-char truncation | **BUG** (design constant, wrong value) |
| Review range prompt ≠ ledger | **BUG** |
| Objective duplicated in prompt | **BUG** |
| `parse_agy_usage()` stub while data exists in the log | **BUG** (small) |
| No cooldown on `RATE_LIMIT`; stated reset time ignored | **CONFIGURATION ISSUE** |
| Degraded-review card states a hardcoded false cause | **BUG** (operator truthfulness) |
| `run_status=SUCCEEDED` for a reviewer that idled | **PROVIDER QUALITY** + **POTENTIAL FUTURE IMPROVEMENT** (effort signals) |
| Planner role contract contradicts objective | **PLANNING QUALITY** |
| No decomposition of a huge objective | **PLANNING QUALITY** |
| Skipped tests counted as PASS | **POTENTIAL FUTURE IMPROVEMENT** |
| Overview blind to repository missions | **UX FRICTION** + **DOGFOOD LIMITATION** |
| Increment 4 repair / recovery / parallelism unexercised | **DOGFOOD LIMITATION** |

---

## 31. Confidence / Limitations

**Directly verified** (read from the database, Git, provider logs, or GG source; several cross-checked by arithmetic):
mission and run identity, all timestamps and durations; the 5 commits and their diffs; write provenance and `independent=0` with its reason; the 131-character `GIT_DIFF` block (decoded character-exactly); the 25,160-character duplicated objective (arithmetic matches the manifest exactly); the 200-character handoff truncation (`engine.py:664` + the handoff file on disk); the review-range mismatch (`latest_candidate_shas` vs `record_review_attempt`, both read in source); findings not carried across retry (both queries filter on `mission_id`; `retry_of_mission_id` used only for linking); the three still-live mission-1 defects (read at HEAD); test counts (re-run: 324+30 skipped, 100 frontend, ruff/mypy clean); AGY's idle transcript and stderr; AGY's unparsed usage object; Claude's quota utilisation 0→16 % and 25→62 %; codex's 118 command executions; opencode's 19-event log; `.orchestrator/` gitignored; no secret-shaped strings in logs.

**Strongly inferred:**
that opencode genuinely authored all 2,661 lines of `d2565e1` — the tree was clean at run start (`dirty_before=0`), the files exist on no other ref, and no other process was running; but its own telemetry stops after 11 seconds, so I cannot see it happen. That the operator would have seen the degraded-review card — I read the component and the API field, but the backend was not running during this review, so I did not observe it rendered.

**Not observable:**
remaining subscription quota for any provider (codex and agy report nothing GG parses; Claude reports a utilisation fraction GG discards); what the operator actually looked at in the browser during the run; why the operator waited ~45 hours before retrying; token counts for 3 of 6 runs.

**Not exercised — no conclusions drawn:**
Increment 4 bounded autonomous repair (0 cycles); parallel/DAG execution, worktrees, sibling isolation, dependency artifacts, integration (all zero rows); Human Gates (0); restart/recovery/stale-claim handling (0); fresh-checkout verification and criterion attempts (0); product lifecycle, plan revisions, phases, waivers, delivery SHA (0); `orchestration_operations` (0). The canary evidence for repair remains the only evidence; this dogfood neither confirms nor contradicts it.

**Scope caveat:** this is one mission, one repository, one night, one execution mode. It is strong evidence about the sequential mission path and near-zero evidence about everything else.

---

## 32. Final Opinion

**Did it actually work well?** Half of it did, and the half that worked is the half that was hard.

I went in expecting to find that the provenance machinery was over-engineered ceremony. It is not. The moment that changed my mind: opencode wrote 2,661 lines across 34 files while emitting nothing to stdout after second eleven, and every commit in this repository is authored `AyhamJo7` regardless of who wrote it. By any normal means that work is unattributable. GG knew exactly what it was, when, from which base, and with which resulting tree — and when it later could not prove that its reviewer was independent, it said so and refused to certify, instead of rounding up. I have reviewed a lot of systems that claim to do that. This one actually did it, unprompted, at 3 a.m., on a repository it had never seen before.

**Was it worth using?** For this run — marginally, yes. You got a real security fix (a decompression-bomb guard that was failing open two different ways), a real financial-correctness fix (banker's rounding silently under-reporting VAT on half the representative restaurant line items — that one is a genuine liability, not a nit), a health probe that can now actually fail, 2,661 lines of coherent new product surface, and an audit document more honest than most humans write. Fifty-one minutes, unattended, one click.

**What annoyed me.** Three things, in order.

The handoff. GG paid Claude for ten minutes and 2.7 million input tokens to produce a superb audit that found the actual root cause of the entire mission — the two feature branches were diverged, not stacked — and then passed downstream a 200-character fragment ending mid-JSON. A `[:200]` nobody has looked at since it was written.

The review. A 131-character context block for a 187 KB diff, over a range that excluded the largest commit, answered by a provider that spent seven minutes waiting for its own background tasks and produced one finding, recorded as `SUCCEEDED`. And then `COMPLETED`, rendering exactly like a clean pass.

And the one that actually bothers me: GG found a hardcoded `"dev-state-secret"` HMAC fallback and an unauthenticated, unthrottled signup endpoint on Wednesday, filed them, fingerprinted them, had Claude re-flag them twice with *"Unchanged since the prior review pass"* — and then, on Friday, because the retry got a new `mission_id`, forgot they existed and reported success. They are still in the code. A review system that un-finds its own findings is worse than no review system, because you stop looking yourself.

**What impressed me.** The refusal to certify. The environment-vs-code classification in mission 1 — recognising that no amount of repair will make a DNS lookup succeed inside a sandbox with no network, and saying so in the persisted reason instead of burning three cycles. Eleven seconds of orchestrator overhead on a fifty-one-minute mission. Honest `UNKNOWN` with a distinct reason code per provider. And mission 1's review→repair loop closing two HIGH findings with file:line evidence while *adding* tests rather than weakening them.

**What I would watch in the next dogfood.** Four things.

Run it as a **product**, not a repository mission — you have never exercised requirements, criteria, phases, fresh-checkout verification, or the Overview the way it was designed, and Operator Experience v1 was built for that path. Run one mission in **`PARALLEL_SAFE`** with a small DAG; Increment 3B is entirely unexercised and this objective was visibly three independent workstreams. Give it an objective that **fits in one mission** — this one was a multi-week program, and much of what looks like GG failing is GG being asked to do something no orchestrator could finish. And when it stops, note **where you looked first**: my strong suspicion is you will open SQLite before you open the browser, and that is the number to drive down.

One last thing worth saying plainly. The instinct to seal the architecture and go dogfood instead of building Increment 5 was correct, and this run proves it — not because everything worked, but because none of the five defects worth fixing were findable from a design document. They were findable in eleven minutes of reading compiled prompts against one real repository. Do not start an increment. Fix the five lines.
