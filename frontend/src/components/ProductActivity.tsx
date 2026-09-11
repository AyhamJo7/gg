import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import { operatorLabel } from "../lib/operator";
import { RunInspector } from "./RunInspector";

export function ProductActivity({ projectId }: { projectId: string }) {
  const { data, error, refresh } = usePolling(() => api.runs().list({ product_project_id: projectId, limit: 50 }), 4000, [projectId]);
  const [runId, setRunId] = useState<string | null>(null);
  return <section className="card section-gap"><h2>Provider activity</h2>
    <p className="muted">Latest 50 recorded invocations, including planning and repair. This is a run history, not a complete project event timeline.</p>
    {error && <p role="alert">Activity updates unavailable. <button onClick={refresh}>Retry</button></p>}
    {data?.runs.map(r => <div className="operator-row" key={r.id}><div><strong>{r.provider} · {r.role}</strong><p className="muted">{new Date(r.started_at).toLocaleString()} · {operatorLabel(r.run_status || r.provider_state)}</p></div>
      <button onClick={() => setRunId(r.id)}>Inspect run</button></div>)}
    {data?.runs.length === 0 && <p className="muted">No invocations recorded for this product yet.</p>}
    {data?.has_more && <p className="muted">Older runs remain available in their missions and the runs API.</p>}
    {runId && <RunInspector key={runId} runId={runId} onClose={() => setRunId(null)} />}
  </section>;
}
