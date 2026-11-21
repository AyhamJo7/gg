# Public release preparation

[← Back to GG](README.md)

## Scope

Prepare the source repository for inspection by other developers. Public visibility,
license selection and distribution of executable artifacts are separate decisions.
Visibility is managed separately through GitHub; this record describes the source inspection and its limits.

## Review record — 2026-11-21

History scanned: every commit up to `a20aacf` on `feature/operator-experience-v2`.
Changes after that commit are not covered; repeat the scan on the release candidate.

| Check | Result |
| --- | --- |
| Git history secret scan | `gitleaks git . --redact --no-banner --report-format json --report-path <tmp>/gitleaks.json --log-level error` exited 1 with three findings |
| Finding inspection | All three inspected at their commits and are synthetic test fixtures (table below) |
| Tracked sensitive/build path inventory | No tracked `.env`, database, `.orchestrator/` runtime directory or `frontend/dist/` paths found in the inspected inventory |
| Runtime packaging | README explicitly prohibits publishing the token-bearing built frontend, local state and raw logs |
| Feature claims | README distinguishes current implementation from proposed redesign and documents local/single-operator scope |
| License | Proprietary rights-reserved notice added in `LICENSE` |
| Functional checks | Gate results per commit are recorded in [NEXT.md](NEXT.md); they are not release proof for a later SHA |

| Rule | File:line | Commit | Content |
| --- | --- | --- | --- |
| `aws-access-token` | `backend/tests/test_git_ops.py:71` | `d9c817071` | placeholder `AKIAABCDEFGHIJKLMNOP` fed to the checkpoint secret filter |
| `generic-api-key` | `backend/tests/test_f05_f06_git_secrets.py:35` | `9be944786` | placeholder `sk-live-1234567890abcdef` planted to prove it is never committed |
| `jwt` | `backend/tests/test_security.py:20` | `edc2718fa` | the public jwt.io sample token fed to `redact()` |

The secret scanner did **not** return a clean result. Test-context inspection explains
its three findings; it does not prove every historical file contains only publishable
information. No broad exclusions or scanner suppressions were added.

Personally identifying information: the author email in commit metadata is part of
history and is a deliberate owner decision, not covered by the secret scan. The copy
in `docs/dogfood/2026-09-12-rechnungsradar.md` was redacted.

## Publication review boundaries

- Review third-party code and notices; the proprietary notice does not supersede their terms.
- Review tracked documents and Git history for employer/client material, private
  prompts, internal URLs and personally identifying information. Automated secret
  scans do not cover all of these categories.
- Repeat the history scan against the exact release candidate. Keep any fixture
  exceptions narrowly documented; investigate new findings.
- Run the documented development checks against the candidate commit. Record the
  command, exit status and commit; do not reuse historical results as release proof.
- Demonstrate the browser workflow with synthetic data. Provider availability and
  real account capacity are separate from fake-provider regression tests.

## Packaging boundaries

Publish source and synthetic fixtures only. Keep `.orchestrator/`, credentials,
provider logs, databases, backups and built frontend artifacts out of releases.
The local bearer token is embedded by Vite during development/build; a local build
is not a reusable public dashboard artifact.

The optional Tauri shell has separate packaging caveats in
[DEVELOPMENT.md](DEVELOPMENT.md). The browser workflow is the documented entry point.

## Suggested repository presentation

Description: **Local coding-agent orchestration with parallel Git worktrees,
review/repair workflows and commit-scoped verification.**

Topics: `coding-agents`, `multi-agent`, `typescript`, `react`, `fastapi`,
`git-worktrees`, `developer-tools`.

A demo should show one synthetic task, separate worktrees, an integration result,
and the final review/verification verdict. Label any fake provider execution and
avoid claiming universal autonomy or guaranteed correctness.
