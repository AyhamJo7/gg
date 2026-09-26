# Operator Experience v2 — progress

Branch: `feature/operator-experience-v2`. Evidence source:
[dogfood/2026-09-12-rechnungsradar.md](dogfood/2026-09-12-rechnungsradar.md) §19
(Operator UX 4/10). Backend semantics stay sealed; additions are read-only
presentation endpoints over persisted records. Unknown is never rendered as zero.

## Tier 1 — truthfulness (dogfood §19)

- [x] T1.1 Degraded-review card states the recorded `degradation_reason`, never a
      hardcoded self-review cause (MissionControlPage).
- [x] T1.2 Mission verdict with caveats: `COMPLETED` with open findings, an
      uncertified review, or no review is not rendered as clean.
- [x] T1.3 Overview treats standalone missions as first-class: attention,
      in-progress and recent outcomes include missions.

## Tier 2 — Agent Relay (who was told what, who handed what to whom)

- [x] T2.1 `GET /api/missions/{id}/relay`: runs + context summary, handoffs
      (bounded, truncation flagged), reviews (writer set, range, independence),
      finding lineage. Read-only.
- [x] T2.2 Relay view: per-provider lanes, chronological steps, thin-context and
      idle/lost-run flags, finding → repair → re-review lineage.

## Tier 3 — live and fast

- [x] T3.1 Global `/ws/events` activity feed + attention notifications.
- [x] T3.2 ⌘K command palette and keyboard navigation.
- [x] T3.3 Retry affordance on the blocking-issue card.

## Tier 4 — polish

- [x] T4.1 DAG with drawn dependency edges.
- [x] T4.2 Consistent SVG icon set, brand mark, clean lint (no warnings).

## Review loop log

(each tier: implement → test → independent review → fix → re-verify)

### Review round 1 (Tiers 1–2) — security: OK TO MERGE; architecture: BLOCK (H1)

Fix plan:

- [x] A-H1 Thin-evidence flag only for named evidence block types (GIT_DIFF,
      TEST_RESULT, FAILURE_EVIDENCE, RELEVANT_CODE, DEPENDENCY_HANDOFF); short
      objectives/criteria never flag.
- [x] A-M1 Split open vs repair-claimed counts so one finding is never listed twice.
- [x] A-M2 `inherited_available=False` when a retry has no readable ancestors.
- [x] A-M3 Trust computed only for terminal missions in the list; bounded queries.
- [x] A-M4 Relay reads handoff prefix + length in SQL, redacts once; relay polls
      only while expanded and mission active.
- [x] A-M5 Truncated blocks carry recorded `representation`/`reason`; no invented
      "budget" cause.
- [x] A-M6 Overview attention capped, stopped before caveated, superseded
      (retried) missions excluded.
- [x] A-M7 Parity test: trust counts vs `open_blockers`/`unverified_findings`
      after a real retry seeding.
- [x] A-L1 Drop unused `inherited_unresolved`.
- [x] A-L2 Legacy unfinished runs → "outcome not recorded", not in flight.
- [x] A-L3 Reviewer-less review spans all lanes.
- [x] A-L4 Trust card wording; unparsed review stated.
- [x] A-L5 Remove non-existent BLOCKED state; PAUSED explicit.
- [x] A-L6 Inherited findings computed once per detail request.
- [x] S-M1/M2 Shared redacting finding serializer for detail findings/inherited;
      redact run summaries and latest handoff on detail.
- [x] S-M3 Relay handoffs/reviews/findings bounded with truncation flags; full
      handoff capped.
- [x] S-L1 Redact and cap finding `file`.
- [x] S-L2 (covered by A-M3).

### Review round 2 — security: OK TO MERGE; architecture: OK TO MERGE

Fixed: split-secret fragment at SQL prefix cuts (margin trim + trailing-token
strip), recent events exclude routine bookkeeping server-side with bounded
payloads and an index (0018), `/ws/events` drops transient output, feed re-seeds
on reconnect and shows loading, each stop counted once with the recorded cause,
"Open retry" for idempotent retries, instant-based timestamp ordering, live
mission titles, DAG arrow tones and no "done" edge into STALE tasks, relay never
shows "0s" for unrecorded durations. Backend 661 passed; frontend 137 passed.

### Review round 3 — security: OK TO MERGE; architecture: OK TO MERGE

Fixed: linear token-tail strip (quadratic regex DoS), formatter churn reverted
from unrelated modules, unread attention counted per mission so parallel-engine
stops (status change only) still count, reconnect gap stated when the re-seed
page cannot cover the outage.

## Remaining dogfood "worth doing" items (§29 below the cut) — all closed

- [x] Degraded-review card states the recorded reason (Tier 1).
- [x] Repository missions on the Overview (Tier 1).
- [x] Provider-stated reset time honored as a rate/quota cooldown floor
      (`classify.parse_reset_after`, `registry.record_failure(stated_reset_s=)`).
- [x] AGY terminal `result.usage` parsed (PARTIAL: thinking/output relation unstated).
- [x] Failover carries the failed attempt's bounded, redacted, "partial and
      unverified" report as FAILURE_EVIDENCE (planner policy included); the
      retry prompt is no longer byte-identical. Backend 677 passed.

### Review round 4 (backend dogfood fixes) — security: OK TO MERGE; architecture: BLOCK (H1) → fixed

- Failover note moved to its own `ContextCompileSpec.failover_text` PREFERRED
  block ("Previous attempt (partial, unverified…)"); `failure_text` is again
  `extra_context[:4000]` byte-for-byte, so observed-vs-expected evidence is
  never displaced. Reviewers never receive it (independence), in both the
  legacy and compiled paths.
- Reset hints: only limit-signal lines, last one wins; day and compound units;
  a 12-hour time without AM/PM takes the sooner reading; capped by
  `orchestration.stated_reset_max_seconds`; the reason is written to
  `last_error`.
- AGY usage: unknown input stays `None`; numeric native counts only.
- Known gap (documented, not built): the parallel DAG task loop and
  `repair_worker` do not yet carry failover notes. Reset-time cooldowns do
  apply there (they live in `invocations.py`). Port only if a dogfood run shows
  duplicated work on those paths.

### Review rounds 5–6 — security: OK TO MERGE; architecture: OK TO MERGE

Reset hints are read only from CLI-written text: structured stream events via
their error payload, never assistant/message/result events; broken JSON-like
lines and the cut first line of a full tail are skipped; the default cap is the
quota floor; wall-clock resets are DST-localized. No open Critical/High/Medium
findings remain on the branch. Accepted Low: weekly limits beyond the cap are
retried at the cap interval (safe floor); parallel/repair-worker failover notes
are a documented gap.

## Follow-up after rounds 5–6

- [x] Parallel DAG task retries carry the previous failed run's report
      (rebuilt from the persisted run, restart-safe; never for review tasks).
      Shared formatting in `failover.py`. Repair worker remains a documented
      gap: no reliable retry→failed-run link without touching sealed
      Increment 4 claim/attempt semantics.
- [x] Skipped-test accounting (migration 0019): pytest/vitest/jest/cargo/mocha
      summary skip counts recorded per verification attempt; exit code stays
      the pass/fail oracle; COMPLETED with skipped tests renders "N tests
      skipped in verification" as a caveat; unreported counts stay unknown.
- [x] Build detection reviewed: GG reads root manifests by design;
      RechnungsRadar's root package.json has no `build` script, so the fix
      belongs in that repository, not an invented filter command in GG.
