import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge } from "../components/Badge";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";

export function LifecyclePage() {
  const { data: projects, refresh } = usePolling(() => api.lifecycle.list(), 4000);
  const [name, setName] = useState("");
  const [idea, setIdea] = useState("");
  const [constraints, setConstraints] = useState("");
  const [targetPath, setTargetPath] = useState("");
  const [autoExecute, setAutoExecute] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setError(null);
    setBusy(true);
    try {
      const created = await api.lifecycle.create({
        name,
        idea,
        constraints,
        auto_execute: autoExecute,
        target_repo_path: targetPath.trim() || undefined,
      });
      setName("");
      setIdea("");
      setConstraints("");
      setTargetPath("");
      refresh();
      window.location.hash = `#/lifecycle/${created.id}`;
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 860 }}>
      <h1>Idea → Product</h1>
      <p className="muted">
        Describe software you want. GG plans the whole product, executes the roadmap with its
        multi-provider engine, reviews and repairs, asks for human setup only when genuinely
        blocked, and delivers a reproducible Git commit.
      </p>
      <div className="card" style={{ marginTop: 12 }}>
        <h3>New Project</h3>
        <div style={{ display: "grid", gap: 8 }}>
          <input
            placeholder="Project name (e.g. Issue Tracker)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            data-testid="lifecycle-name"
          />
          <textarea
            placeholder="Describe the software you want: users, core journeys, must-have features…"
            value={idea}
            onChange={(e) => setIdea(e.target.value)}
            rows={4}
            data-testid="lifecycle-idea"
          />
          <textarea
            placeholder="Optional constraints (stack preferences, things to avoid, accounts you already have)…"
            value={constraints}
            onChange={(e) => setConstraints(e.target.value)}
            rows={2}
            data-testid="lifecycle-constraints"
          />
          <input
            placeholder="Target repository path (optional — GG creates one otherwise)"
            value={targetPath}
            onChange={(e) => setTargetPath(e.target.value)}
            data-testid="lifecycle-target"
          />
          <label className="row" style={{ gap: 8 }}>
            <input type="checkbox" checked={autoExecute} onChange={(e) => setAutoExecute(e.target.checked)} />
            <span className="muted">Automatic execution: start phases as soon as a valid plan exists</span>
          </label>
          <div>
            <button className="primary" onClick={create} disabled={!name.trim() || !idea.trim() || busy} data-testid="lifecycle-create">
              {busy ? "Creating…" : "Create Project"}
            </button>
          </div>
          {error && <p style={{ color: "var(--red)" }}>{error}</p>}
        </div>
      </div>
      <div style={{ marginTop: 16 }}>
        {projects?.map((p) => (
          <div className="list-row" key={p.id} data-testid="lifecycle-row">
            <div>
              <Link to={`/lifecycle/${p.id}`} style={{ fontWeight: 600 }} data-testid="lifecycle-open">
                {p.name}
              </Link>
              <div className="muted" style={{ fontSize: 12 }}>
                plan rev {p.plan_revision} · {Object.entries(p.phase_counts).map(([k, v]) => `${v}× ${k}`).join(", ") || "no phases yet"}
                {p.open_gates > 0 && ` · ${p.open_gates} open gate(s)`}
              </div>
              {p.blocking_reason && <div style={{ fontSize: 12, color: "var(--red)" }}>{p.blocking_reason}</div>}
            </div>
            <div className="row">
              <Badge value={p.state} />
              {p.delivery_sha && <span className="mono muted" style={{ fontSize: 11 }}>{p.delivery_sha.slice(0, 8)}</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
