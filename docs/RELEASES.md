# Releases

## v2.1-scale (promoted 2026-09-08)

Scale increment: unattended 6-task mission proof plus audit repairs.

| Item | Value |
|------|-------|
| Merge commit | `5faa89687918bba962aeb9af43954731ae17264a` |
| Approved head | `869f06178dddae769abff13dac5fb8bf77e5a311` |
| PR | `AyhamJo7/gg#2` (merge, lineage preserved, no squash) |
| Release tag | `v2.1-scale` |
| Audit verdict | READY (final checkpoint + leak-audit repairs reviewed) |

Verification on the promoted merge commit (rerun, all exit 0):

| Suite | Result |
|-------|--------|
| Backend `pytest` | 268 passed |
| Frontend `vitest --run` | 41 passed |
| `mypy --strict src` | clean (36 files) |
| `ruff check src tests` | clean |
| `tsc -b --noEmit` | clean |
| `eslint` | 0 errors, 1 pre-existing warning |
| `vite build` | successful |

Scale evidence (preserved, not rewritten): mission `f81c7a9662c54965`
(`textstats`, 6 tasks) in the mission database; target repo
`/tmp/gg-scale-textstats` with recorded `git_head 26dd327` (pre-repair,
historical) — repaired-engine behavior proven deterministically by
`test_final_checkpoint.py`. Leak audit clean via `scripts/leak-audit.py`;
driver in `scripts/e2e-scale-dogfood.js`, plan in `/tmp/gg-scale/plan.json`.

## v2.0-core (promoted 2026-09-08)

First promoted baseline beyond the certified v1 core. Merges the independently
reviewed Phase 2A parallel scheduler, Phase 2B parallel-mission UI, Phase 2C
operator workflow, and the redaction-context hardening increment.

| Item | Value |
|------|-------|
| Merge commit | `637914a7772f62f26cedfa1183f2501f987902e6` |
| Approved head | `aaab109c1e6b979ea3026ec2dabd36d0666c1c05` |
| PR | `AyhamJo7/gg#1` (merge, lineage preserved, no squash) |
| Release tag | `v2.0-core` |
| Milestone tags | `v1.0-core-certified`, `phase2b-approved`, `phase2c-approved` |
| Audit verdict | READY (all Phase 2A–2C findings closed by AGY review) |

Verification on the promoted merge commit (rerun, all exit 0):

| Suite | Result |
|-------|--------|
| Backend `pytest` | 256 passed |
| Frontend `vitest --run` | 41 passed |
| `mypy --strict src` | clean (36 files) |
| `ruff check src tests` | clean |
| `tsc -b --noEmit` | clean |
| `eslint` | 0 errors, 1 pre-existing warning |
| `vite build` | successful |

Closure details live in `docs/PHASE2A_REPAIR_REPORT.md`, `docs/PHASE2B_REPORT.md`,
and the Phase 2C closure report on `phase2c/operator-workflow` (`b1379fd`).

### Known limitations carried forward

- Redaction supports declared formats within stated bounds; wider
  anchor/value separations are explicitly unsupported (see `SECURITY.md`).
- Live runs exercised `agy`/`opencode` only; config remains limited accordingly.
- HTTP API is localhost-only by design; raw on-disk logs are unredacted by
  design (`SECURITY.md`).
- Scale beyond trivial DAGs and sustained multi-hour unattended operation are
  unproven (see roadmap proposal below).
