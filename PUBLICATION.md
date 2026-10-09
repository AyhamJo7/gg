# Public release preparation

[← Back to GG](README.md)

## Scope

Prepare the source repository for inspection by other developers. Public visibility,
license selection and distribution of executable artifacts are separate decisions.
Visibility is managed separately through GitHub; this record describes the source inspection and its limits.

## Review record — 2026-10-09

Source HEAD inspected: `0f81b421f394db98a4661b3c780d60ecd40258b7`.
README and publication preparation changes are uncommitted and not covered by that
HEAD identifier.

| Check | Result |
| --- | --- |
| Git history secret scan | `gitleaks git /home/adam/projects/gg --redact --no-banner --report-format json --report-path /tmp/gg-publication-gitleaks.json --log-level error` exited 1 with three findings |
| Finding inspection | Historical source context places all three in synthetic security test fixtures: AWS-shaped key, generic API-key sample and JWT sample |
| Tracked sensitive/build path inventory | No tracked `.env`, database, `.orchestrator/` runtime directory or `frontend/dist/` paths found in the inspected inventory |
| Runtime packaging | README explicitly prohibits publishing the token-bearing built frontend, local state and raw logs |
| Feature claims | README distinguishes current implementation from proposed redesign and documents local/single-operator scope |
| License | Proprietary rights-reserved notice added in `LICENSE` |
| Functional checks | No fresh application build, lint, typecheck or test result established by this documentation work |

The secret scanner did **not** return a clean result. Test-context inspection explains
its three findings; it does not prove every historical file contains only publishable
information. No broad exclusions or scanner suppressions were added.

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
