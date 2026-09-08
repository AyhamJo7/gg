import { Badge } from "./Badge";
import type { IntegrationRecord } from "../lib/types";

export function IntegrationPanel({ integration }: { integration: IntegrationRecord | null }) {
  if (!integration) {
    return (
      <div className="card">
        <h3>Integration</h3>
        <p className="muted">Integration has not started yet.</p>
      </div>
    );
  }

  const isConflict = integration.status === "MERGE_CONFLICT";
  const isFailed = integration.status === "FAILED";

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

  return (
    <div className="card" style={{ borderColor: isConflict || isFailed ? "var(--red)" : undefined }} data-testid="integration-panel">
      <div className="row spread" style={{ marginBottom: 8 }}>
        <h3>Integration</h3>
        <Badge value={integration.status} />
      </div>
      {integration.merged_commit && (
        <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
          Merged commit: <span className="mono">{integration.merged_commit}</span>
        </div>
      )}
      {branches.length > 0 && (
        <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
          Branches: {branches.join(", ")}
        </div>
      )}
      {integration.summary && (
        <div className="muted" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>{integration.summary}</div>
      )}
      {isConflict && conflictFiles.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontWeight: 600, fontSize: 12, color: "var(--red)", marginBottom: 4 }}>Conflict files:</div>
          {conflictFiles.map((f) => (
            <div key={f} className="mono" style={{ fontSize: 11, color: "var(--red)" }}>
              {f}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
