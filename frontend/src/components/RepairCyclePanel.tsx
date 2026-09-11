import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import { ACTIVE_REPAIR_STATES, operatorLabel } from "../lib/operator";
import { RunInspector } from "./RunInspector";

export function RepairCyclePanel({ projectId, refresh }: {
  projectId: string; refresh: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const { data, error, refresh: reload } = usePolling(() => api.lifecycle.repairCycles(projectId), 2500, [projectId]);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  if (!data && !error) return <p className="muted" role="status">Checking repair activity…</p>;
  if (error) return <p role="alert">Repair updates unavailable; status may be stale. <button onClick={reload}>Retry</button></p>;
  if (!data?.cycles.length) return null;
  return <section className="card" data-testid="repair-panel" aria-label="Autonomous repair">
    <h2>Autonomous repair</h2>
    {actionError && <p role="alert">{actionError}</p>}
    {data.cycles.map(c => {
      const active = ACTIVE_REPAIR_STATES.has(c.status);
      const stopped = ["EXHAUSTED", "FAILED", "BLOCKED", "WAITING_FOR_HUMAN"].includes(c.status);
      return <article className="repair-cycle" key={c.id}>
        <div className="row spread"><strong>{c.target_criterion_id || c.target_finding_id || "Acceptance check"}</strong>
          <span className={`badge ${c.status === "SUCCEEDED" ? "green" : stopped ? "red" : "yellow"}`}>{operatorLabel(c.status)}</span></div>
        <p>{c.classification === "IMPLEMENTATION_DEFECT" ? "A code defect was identified from verification evidence." : `Classification: ${operatorLabel(c.classification)}.`}</p>
        <p className="muted">{c.attempts_used} of {c.max_attempts} attempts used · {Math.max(0, c.max_attempts - c.attempts_used)} remaining</p>
        {(c.attempts ?? []).map(a => <div key={a.id} className="repair-attempt">
          <strong>Attempt {a.attempt_number} · {a.provider || "Provider not assigned"}</strong>
          <ol className="trust-steps">
            <li>Repair: {a.result_sha ? "change recorded" : "no result recorded yet"}</li>
            <li>{a.review_reviewer ? `Independently reviewed by ${a.review_reviewer} (${a.review_outcome})` : "Independent review not recorded yet"}</li>
            <li>{a.recheck_outcome ? `Exact recheck ${a.recheck_outcome}` : "Exact recheck not recorded yet"}</li>
          </ol>
          {a.provider_run_id && <button onClick={() => setRunId(a.provider_run_id!)}>Inspect repair run</button>}
        </div>)}
        {c.stop_reason && <p className="notice">{c.stop_reason}</p>}
        <p className="muted">{c.status === "SUCCEEDED" ? "Repair passed. GG returns this candidate to acceptance; this is not delivery." :
          stopped ? "Your decision is needed. Inspect the failed evidence and any Human Gate before changing the plan or attempting more work." :
            c.status === "WAITING_FOR_PROVIDER" ? "GG will retry provider availability automatically. No code attempt is running while it waits." :
              active ? "GG handles the next stage automatically. You can leave this page open; status refreshes." : "This repair is no longer running. Its evidence remains available below."}</p>
        {active && <button disabled={busy !== null} data-testid={`cancel-repair-${c.id}`} onClick={async () => {
          if (!window.confirm("Cancel this repair cycle? Completed work and evidence are preserved.")) return;
          setBusy(c.id); setActionError(null);
          try { await refresh(() => api.lifecycle.cancelRepairCycle(projectId, c.id)); reload(); }
          catch (e) { setActionError(e instanceof Error ? e.message : String(e)); }
          finally { setBusy(null); }
        }}>{busy === c.id ? "Cancelling…" : "Cancel repair"}</button>}
        <details><summary>Technical lineage</summary><dl className="technical-details">
          <dt>Cycle</dt><dd>{c.id}</dd><dt>State</dt><dd>{c.status}</dd>
          <dt>Classification</dt><dd>{c.classification}</dd><dt>Trigger SHA</dt><dd>{c.trigger_sha}</dd>
        </dl>
          {(c.attempts ?? []).map(a => <p className="mono" key={a.id}>Attempt {a.attempt_number}: {a.base_sha} → {a.result_sha || "No result"} · {a.outcome}<br/>Recheck attempt: {a.recheck_attempt_id || "Not recorded"}</p>)}
        </details>
      </article>;
    })}
    {runId && <RunInspector key={runId} runId={runId} onClose={() => setRunId(null)} />}
  </section>;
}
