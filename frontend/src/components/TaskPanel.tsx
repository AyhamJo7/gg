import { Badge } from "./Badge";
import { api } from "../lib/api";
import type { TaskRecord } from "../lib/types";
import { useState } from "react";

export function TaskPanel({
  task,
  missionId,
  onRefresh,
}: {
  task: TaskRecord;
  missionId: string;
  onRefresh: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isTerminal = ["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED"].includes(task.status);
  const isRunning = ["RUNNING", "CLAIMED", "WAITING_FOR_PROVIDER"].includes(task.status);

  const retry = async () => {
    setBusy(true); setError(null);
    try { await api.missions.retryTask(missionId, task.id); onRefresh(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const cancel = async () => {
    if (!window.confirm("Cancel this task? Completed artifacts are preserved.")) return;
    setBusy(true); setError(null);
    try { await api.missions.cancelTask(missionId, task.id); onRefresh(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const scope = (() => {
    try {
      return JSON.parse(task.workspace_scope || "[]");
    } catch {
      return [];
    }
  })();

  const preferred = (() => {
    try {
      return JSON.parse(task.preferred_providers || "[]");
    } catch {
      return [];
    }
  })();

  return (
    <div className="card" style={{ marginBottom: 8 }} data-testid={`task-panel-${task.id}`}>
      <div className="row spread" style={{ marginBottom: 8 }}>
        <div className="row" style={{ gap: 10 }}>
          <Badge value={task.status} pulse={isRunning} />
          <span style={{ fontWeight: 600 }}>{task.title || task.role}</span>
        </div>
        <div className="row">
          {!isTerminal && (
            <button className="danger" style={{ padding: "3px 8px", fontSize: 11 }} onClick={cancel} disabled={busy}>
              Cancel
            </button>
          )}
          {(task.status === "FAILED" || task.status === "CANCELLED" || task.status === "STALE") && (
            <button disabled={busy} style={{ padding: "3px 8px", fontSize: 11 }} onClick={retry}>
              Retry
            </button>
          )}
        </div>
      </div>
      {task.description && <p className="muted" style={{ fontSize: 12, margin: "4px 0 8px" }}>{task.description}</p>}
      {error && <p role="alert">{error}</p>}
      {task.status === "STALE" && <p className="notice">A dependency changed. Retry creates a fresh attempt; old results stay in history.</p>}
      <details><summary>Provider, scope & exact artifacts</summary><p className="mono">Task: {task.id}</p>
      <div className="row" style={{ gap: 16, flexWrap: "wrap" }}>
        {task.assigned_provider && (
          <span className="muted" style={{ fontSize: 11 }}>
            Provider: <span className="mono">{task.assigned_provider}</span>
          </span>
        )}
        {preferred.length > 0 && (
          <span className="muted" style={{ fontSize: 11 }}>
            Preferred: <span className="mono">{preferred.join(", ")}</span>
          </span>
        )}
        {scope.length > 0 && (
          <span className="muted" style={{ fontSize: 11 }}>
            Scope: <span className="mono">{scope.join(", ")}</span>
          </span>
        )}
        {task.started_at && (
          <span className="muted" style={{ fontSize: 11 }}>
            Started: {new Date(task.started_at).toLocaleTimeString()}
          </span>
        )}
        {task.checkpoint_after && (
          <span className="muted" style={{ fontSize: 11 }}>
            Checkpoint: <span className="mono">{task.checkpoint_after.slice(0, 7)}</span>
          </span>
        )}
        {task.input_sha && (
          <span className="muted" style={{ fontSize: 11 }} title="Exact pinned input artifact for this attempt">
            Input: <span className="mono">{task.input_sha.slice(0, 7)}</span>
          </span>
        )}
        {task.result_sha && (
          <span className="muted" style={{ fontSize: 11 }} title="Immutable result artifact for this attempt">
            Output: <span className="mono">{task.result_sha.slice(0, 7)}</span>
          </span>
        )}
      </div>
      <p className="mono">Input SHA: {task.input_sha || "Not recorded"}<br/>Result SHA: {task.result_sha || "Not recorded"}</p>
      </details>
      {task.blocking_issue && (
        <div style={{ marginTop: 6, fontSize: 11, color: "var(--red)" }}>{task.blocking_issue}</div>
      )}
      {task.summary && (
        <div className="muted" style={{ marginTop: 6, fontSize: 12, whiteSpace: "pre-wrap" }}>
          {task.summary}
        </div>
      )}
    </div>
  );
}
