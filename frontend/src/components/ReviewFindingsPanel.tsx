import { Badge } from "./Badge";
import type { ReviewFinding } from "../lib/types";

export function ReviewFindingsPanel({ findings }: { findings: ReviewFinding[] }) {
  const openFindings = findings.filter((f) => f.status === "open");
  const blockerHigh = openFindings.filter((f) => f.severity === "BLOCKER" || f.severity === "HIGH");

  return (
    <div className="card" data-testid="review-findings-panel">
      <div className="row spread" style={{ marginBottom: 8 }}>
        <h3>Review Findings</h3>
        {blockerHigh.length > 0 && (
          <Badge value={`${blockerHigh.length} blocker/high`} />
        )}
      </div>
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
                <span className="faint" style={{ fontSize: 11, textTransform: "capitalize" }}>{f.status}</span>
              </div>
              <div style={{ fontSize: 12.5, marginBottom: 4 }}>{f.description}</div>
              {f.recommended_fix && (
                <div className="muted" style={{ fontSize: 11 }}>
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
