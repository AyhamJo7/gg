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

- [ ] T3.1 Global `/ws/events` activity feed + attention notifications.
- [ ] T3.2 ⌘K command palette and keyboard navigation.
- [ ] T3.3 Retry affordance on the blocking-issue card.

## Tier 4 — polish

- [ ] T4.1 DAG with drawn dependency edges.
- [ ] T4.2 Consistent icons / type scale / mission picker.

## Review loop log

(each tier: implement → test → independent review → fix → re-verify)

### Review round 1 (Tiers 1–2) — security: OK TO MERGE; architecture: BLOCK (H1)

Fix plan:

- [ ] A-H1 Thin-evidence flag only for named evidence block types (GIT_DIFF,
      TEST_RESULT, FAILURE_EVIDENCE, RELEVANT_CODE, DEPENDENCY_HANDOFF); short
      objectives/criteria never flag.
- [ ] A-M1 Split open vs repair-claimed counts so one finding is never listed twice.
- [ ] A-M2 `inherited_available=False` when a retry has no readable ancestors.
- [ ] A-M3 Trust computed only for terminal missions in the list; bounded queries.
- [ ] A-M4 Relay reads handoff prefix + length in SQL, redacts once; relay polls
      only while expanded and mission active.
- [ ] A-M5 Truncated blocks carry recorded `representation`/`reason`; no invented
      "budget" cause.
- [ ] A-M6 Overview attention capped, stopped before caveated, superseded
      (retried) missions excluded.
- [ ] A-M7 Parity test: trust counts vs `open_blockers`/`unverified_findings`
      after a real retry seeding.
- [ ] A-L1 Drop unused `inherited_unresolved`.
- [ ] A-L2 Legacy unfinished runs → "outcome not recorded", not in flight.
- [ ] A-L3 Reviewer-less review spans all lanes.
- [ ] A-L4 Trust card wording; unparsed review stated.
- [ ] A-L5 Remove non-existent BLOCKED state; PAUSED explicit.
- [ ] A-L6 Inherited findings computed once per detail request.
- [ ] S-M1/M2 Shared redacting finding serializer for detail findings/inherited;
      redact run summaries and latest handoff on detail.
- [ ] S-M3 Relay handoffs/reviews/findings bounded with truncation flags; full
      handoff capped.
- [ ] S-L1 Redact and cap finding `file`.
- [ ] S-L2 (covered by A-M3).
