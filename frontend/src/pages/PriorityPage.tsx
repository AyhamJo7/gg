import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import type { PriorityMatrix } from "../lib/types";

const ROLE_LABELS: Record<string, string> = {
  planning: "Planning",
  implementation: "Implementation",
  testing: "Testing",
  review: "Review",
  repair: "Repair",
};

export function PriorityPage() {
  const { data: matrix, refresh } = usePolling(() => api.settings.priority(), null);
  const { data: profiles, refresh: refreshProfiles } = usePolling(() => api.settings.profiles(), null);
  const [drag, setDrag] = useState<{ role: string; provider: string } | null>(null);
  const [profileName, setProfileName] = useState("");

  const move = async (role: string, fromProvider: string, toProvider: string) => {
    if (!matrix || fromProvider === toProvider) return;
    const list = [...(matrix[role] ?? [])];
    const from = list.indexOf(fromProvider);
    const to = list.indexOf(toProvider);
    if (from === -1 || to === -1) return;
    list.splice(to, 0, ...list.splice(from, 1));
    await api.settings.setPriority(role, list);
    refresh();
  };

  const saveProfile = async () => {
    if (!matrix || !profileName.trim()) return;
    await api.settings.saveProfile(profileName.trim(), matrix as PriorityMatrix);
    setProfileName("");
    refreshProfiles();
  };

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Priority Matrix</h1>
          <p className="muted">Drag providers to reorder per-phase preference. The top eligible provider is selected first.</p>
        </div>
        <div className="row">
          <input
            aria-label="Profile name"
            placeholder="profile name"
            value={profileName}
            onChange={(e) => setProfileName(e.target.value)}
            style={{ maxWidth: 160 }}
          />
          <button className="primary" onClick={saveProfile} disabled={!profileName.trim()}>Save profile</button>
        </div>
      </div>
      {profiles && Object.keys(profiles).length > 0 && (
        <div className="row" style={{ marginBottom: 12, flexWrap: "wrap" }}>
          <span className="muted">Saved:</span>
          {Object.keys(profiles).map((p) => (
            <span key={p} className="badge">{p}</span>
          ))}
        </div>
      )}
      <div className="matrix">
        {Object.entries(ROLE_LABELS).map(([role, label]) => (
          <div className="card matrix-col" key={role}>
            <h3>{label}</h3>
            <div className="matrix-list" data-testid={`matrix-${role}`}>
              {(matrix?.[role] ?? []).map((provider, idx) => (
                <div
                  key={provider}
                  className={`matrix-item ${drag?.provider === provider && drag.role === role ? "dragging" : ""}`}
                  draggable
                  onDragStart={() => setDrag({ role, provider })}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => {
                    if (drag && drag.role === role) move(role, drag.provider, provider);
                    setDrag(null);
                  }}
                >
                  <span className="rank">{idx + 1}</span>
                  <span>⠿</span>
                  <span>{provider}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
