import { useState } from "react";
import { api } from "../lib/api";
import type { HumanGate } from "../lib/types";

export function GateCard({ gate, missionId, onResolved }: { gate: HumanGate; missionId: string; onResolved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [adopted, setAdopted] = useState<string | null>(null);
  if (gate.status !== "open") return null;
  const isAttributionGate = /unattributed|adopt/i.test(`${gate.reason} ${gate.detail ?? ""}`);
  const resolve = async (choice: string) => {
    setBusy(true);
    try {
      await api.missions.resolveGate(missionId, gate.id, choice);
      onResolved();
    } finally {
      setBusy(false);
    }
  };
  const adopt = async () => {
    setBusy(true);
    try {
      const result = await api.missions.adoptChanges(missionId);
      setAdopted(
        result.adopted ? `Adopted as HUMAN_OPERATOR at ${(result.result_sha ?? "").slice(0, 8)}` : "Nothing to adopt",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card gate-card" data-testid="human-gate">
      <h2>⚠ Human decision required</h2>
      <p style={{ margin: "4px 0", fontWeight: 550 }}>{gate.reason}</p>
      {gate.detail && <pre className="muted" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{gate.detail}</pre>}
      <div className="row" style={{ marginTop: 10, flexWrap: "wrap" }}>
        {isAttributionGate && (
          <button className="primary" disabled={busy} onClick={adopt} data-testid="adopt-changes">
            Adopt workspace changes as human
          </button>
        )}
        {gate.choices.map((choice) => (
          <button
            key={choice}
            className={choice === gate.recommended && !isAttributionGate ? "primary" : ""}
            disabled={busy}
            onClick={() => resolve(choice)}
          >
            {choice}
            {choice === gate.recommended ? " (recommended)" : ""}
          </button>
        ))}
      </div>
      {adopted && <p className="muted" style={{ fontSize: 12 }}>{adopted} — then resolve the gate to continue.</p>}
    </div>
  );
}
