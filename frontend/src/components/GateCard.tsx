import { useState } from "react";
import { api } from "../lib/api";
import type { HumanGate } from "../lib/types";

export function GateCard({ gate, missionId, onResolved }: { gate: HumanGate; missionId: string; onResolved: () => void }) {
  const [busy, setBusy] = useState(false);
  if (gate.status !== "open") return null;
  const resolve = async (choice: string) => {
    setBusy(true);
    try {
      await api.missions.resolveGate(missionId, gate.id, choice);
      onResolved();
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
        {gate.choices.map((choice) => (
          <button
            key={choice}
            className={choice === gate.recommended ? "primary" : ""}
            disabled={busy}
            onClick={() => resolve(choice)}
          >
            {choice}
            {choice === gate.recommended ? " (recommended)" : ""}
          </button>
        ))}
      </div>
    </div>
  );
}
