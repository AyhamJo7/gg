import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";

export function NewMissionPage() {
  const navigate = useNavigate();
  const { data: projects } = usePolling(() => api.projects.list(), null);
  const { data: profiles } = usePolling(() => api.settings.profiles(), null);
  const [projectId, setProjectId] = useState("");
  const [title, setTitle] = useState("");
  const [task, setTask] = useState("");
  const [autonomy, setAutonomy] = useState("BALANCED");
  const [profile, setProfile] = useState("balanced");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.missions.create({ project_id: projectId, title, task, autonomy, profile, start: true });
      navigate("/");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 640 }}>
      <h1>New Mission</h1>
      <p className="muted">One high-level engineering task. The orchestrator plans, executes, reviews, and verifies it across your AI subscriptions.</p>
      <div className="card stack" style={{ marginTop: 16 }}>
        <div className="field">
          <label>Project</label>
          {projects && projects.length > 0 ? (
            <select value={projectId} onChange={(e) => setProjectId(e.target.value)} data-testid="project-select">
              <option value="">Select a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name} ({p.path})</option>
              ))}
            </select>
          ) : (
            <p className="muted">No projects yet — add one on the Projects page first.</p>
          )}
        </div>
        <div className="field">
          <label>Mission title</label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Build invoice management SaaS" />
        </div>
        <div className="field">
          <label>Task description</label>
          <textarea
            rows={6}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            placeholder="Describe the outcome you want. Include requirements, constraints, and acceptance criteria."
            data-testid="task-input"
          />
        </div>
        <div className="grid-2">
          <div className="field">
            <label>Autonomy level</label>
            <select value={autonomy} onChange={(e) => setAutonomy(e.target.value)}>
              <option value="SAFE">SAFE — confirm before implementation</option>
              <option value="BALANCED">BALANCED — gates on real decisions</option>
              <option value="AUTONOMOUS">AUTONOMOUS — only hard gates</option>
            </select>
          </div>
          <div className="field">
            <label>Execution profile</label>
            <select value={profile} onChange={(e) => setProfile(e.target.value)}>
              <option value="balanced">Balanced</option>
              {Object.keys(profiles ?? {}).map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>
        </div>
        {error && <p style={{ color: "var(--red)" }}>{error}</p>}
        <div>
          <button className="primary" disabled={busy || !projectId || !title || !task} onClick={submit} data-testid="launch-mission">
            {busy ? "Launching…" : "▶ Launch Mission"}
          </button>
        </div>
      </div>
    </div>
  );
}
