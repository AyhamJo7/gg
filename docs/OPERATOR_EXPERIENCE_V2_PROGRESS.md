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
