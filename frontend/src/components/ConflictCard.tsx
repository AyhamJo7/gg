import { useState } from "react";
import { api } from "../lib/api";
import type { IntegrationRecord } from "../lib/types";

/** Operator workflow for a blocked integration (merge conflict).
 *
 * The backend never auto-resolves: task branches are preserved and the
 * mission waits. The operator merges the listed branches in the project
 * repo, then resumes — resume re-runs integration, skipping branches that
 * are already merged. Nothing is discarded or force-resolved.
 */
export function ConflictCard({
  integration,
  missionId,
  onResumed,
}: {
  integration: IntegrationRecord;
  missionId: string;
  onResumed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  let conflictFiles: string[] = [];
  try {
    conflictFiles = JSON.parse(integration.conflict_files || "[]");
  } catch {
    conflictFiles = [];
  }

  let branches: string[] = [];
  try {
    branches = JSON.parse(integration.branch_names || "[]");
  } catch {
    branches = [];
  }

  const resume = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.missions.action(missionId, "resume");
      onResumed();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" style={{ borderColor: "var(--red)" }} data-testid="conflict-card">
      <h3>⚠ Merge conflict — action required</h3>
      <p className="muted" style={{ fontSize: 12 }}>
        Integration is blocked. Task branches are preserved; nothing was discarded.
        Merge the branches below in the project repository, resolving each listed file,
        then resume — already-merged branches are skipped automatically.
      </p>
      {branches.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>Task branches:</div>
          {branches.map((b) => (
            <div key={b} className="mono" style={{ fontSize: 11 }}>{b}</div>
          ))}
        </div>
      )}
      {conflictFiles.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4, color: "var(--red)" }}>
            Conflicting files:
          </div>
          {conflictFiles.map((f) => (
            <div key={f} className="mono" style={{ fontSize: 11, color: "var(--red)" }}>
              {f}
            </div>
          ))}
        </div>
      )}
      {integration.summary && (
        <p className="muted" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>{integration.summary}</p>
      )}
      <div style={{ marginTop: 8 }}>
        <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>Suggested resolution:</div>
        <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap" }}>
{`git status   # confirm a clean tree on the main branch
${branches.map((b) => `git merge --no-commit --no-ff ${b}`).join("\n")}
# resolve each conflicting file, then:
git add <resolved files> && git commit
# back here, press Resume integration`}
        </pre>
      </div>
      {error && <p style={{ color: "var(--red)", fontSize: 12 }}>Resume failed: {error}</p>}
      <div style={{ marginTop: 8 }}>
        <button className="primary" onClick={resume} disabled={busy} data-testid="conflict-resume">
          {busy ? "Resuming…" : "▶ Resume integration"}
        </button>
      </div>
    </div>
  );
}
