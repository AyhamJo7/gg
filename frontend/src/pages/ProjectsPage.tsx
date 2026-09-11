import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import { Badge } from "../components/Badge";

export function ProjectsPage() {
  const { data: projects, error: loadError, refresh } = usePolling(() => api.projects.list(), null);
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [validation, setValidation] = useState<Record<string, unknown> | null>(null);

  const add = async () => {
    setError(null);
    try {
      await api.projects.add(path);
      setPath("");
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const validate = async (id: string) => {
    setError(null);
    try { const result = await api.projects.validate(id); setValidation(result as unknown as Record<string, unknown>); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div style={{ maxWidth: 760 }}>
      <h1>Repositories</h1>
      <p className="muted">Registered workspaces for missions. Paths are validated and checkpoints screen for recognized secrets.</p>
      {loadError && <p role="alert">Repositories unavailable. <button onClick={refresh}>Retry</button></p>}
      <div className="card" style={{ marginTop: 12 }}>
        <div className="row">
          <input
            aria-label="Repository path"
            placeholder="/absolute/path/to/project"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            data-testid="project-path-input"
          />
          <button className="primary" onClick={add} disabled={!path}>Add repository</button>
        </div>
        {error && <p role="alert" style={{ color: "var(--red)" }}>{error}</p>}
      </div>
      <div style={{ marginTop: 12 }}>
        {projects?.map((p) => (
          <div className="list-row" key={p.id} data-testid="project-row">
            <div>
              <div style={{ fontWeight: 600 }}>{p.name}</div>
              <div className="mono muted" style={{ fontSize: 11.5 }}>{p.path}</div>
            </div>
            <div className="row">
              <Badge value={p.detected_type || "unknown"} />
              <button onClick={() => validate(p.id)}>Validate</button>
              <button className="danger" onClick={async () => {
                if (!window.confirm(`Remove repository registration for ${p.name}? Files stay on disk. GG will refuse if this repository has missions.`)) return;
                setError(null);
                try { await api.projects.remove(p.id); refresh(); }
                catch (e) { setError(e instanceof Error ? e.message : String(e)); }
              }}>Remove</button>
            </div>
          </div>
        ))}
      </div>
      {validation && (
        <div className="card" style={{ marginTop: 12 }}>
          <h3>Validation</h3>
          <pre className="muted" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>
            {JSON.stringify(validation, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
