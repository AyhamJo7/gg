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

- [ ] T2.1 `GET /api/missions/{id}/relay`: runs + context summary, handoffs
      (bounded, truncation flagged), reviews (writer set, range, independence),
      finding lineage. Read-only.
- [ ] T2.2 Relay view: per-provider lanes, chronological steps, thin-context and
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
