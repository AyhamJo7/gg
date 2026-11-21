import { Badge } from "./Badge";
import type { ReviewFinding } from "../lib/types";

const UNRESOLVED = new Set(["open", "repair_attempted"]);

/** Local findings plus unresolved retry-inherited ones. `inherited` is null
 * when the backend could not read retry history: said, not hidden. */
export function ReviewFindingsPanel({ findings: local, inherited = [] }: { findings: ReviewFinding[]; inherited?: ReviewFinding[] | null }) {
  const findings = [...local, ...(inherited ?? [])];
  const openFindings = findings.filter((f) => f.status === "open");
  const unresolved = findings.filter((f) => UNRESOLVED.has(f.status)).length;
  const blockerHigh = openFindings.filter((f) => f.severity === "BLOCKER" || f.severity === "HIGH");

  return (
    <div className="card" data-testid="review-findings-panel">
      <div className="row spread" style={{ marginBottom: 8 }}>
        <h3>Review Findings</h3>
        <span className="row">
          {unresolved > 0 && <span className="muted">{unresolved} unresolved</span>}
          {blockerHigh.length > 0 && <Badge value={`${blockerHigh.length} blocker/high`} />}
        </span>
      </div>
      {inherited === null && <p className="notice">Findings inherited from earlier attempts could not be loaded.</p>}
      {findings.length === 0 ? (
        <p className="muted">No review findings recorded.</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {findings.map((f) => (
            <div
              key={f.id}
              className="card"
              style={{
                padding: 10,
                borderColor:
                  f.severity === "BLOCKER"
                    ? "var(--red)"
                    : f.severity === "HIGH"
                      ? "var(--orange)"
                      : undefined,
              }}
            >
              <div className="row spread" style={{ marginBottom: 4 }}>
                <div className="row">
                  <Badge value={f.severity} />
                  {f.file && <span className="mono faint" style={{ fontSize: 11 }}>{f.file}</span>}
                </div>
                <span className="faint" style={{ fontSize: 11 }}>
                  {f.status === "repair_attempted" ? "Repair claimed · not verified" : f.status}
                  {f.inherited_from_mission_id && <> · inherited from {f.inherited_from_mission_id.slice(0, 8)}</>}
                </span>
              </div>
              <div style={{ fontSize: 12.5, marginBottom: 4, overflowWrap: "anywhere" }}>{f.description}</div>
              {f.recommended_fix && (
                <div className="muted" style={{ fontSize: 11, overflowWrap: "anywhere" }}>
                  Fix: {f.recommended_fix}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
