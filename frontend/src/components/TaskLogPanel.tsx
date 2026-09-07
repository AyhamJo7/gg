import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { ProviderRun } from "../lib/types";

export function TaskLogPanel({ missionId, taskId }: { missionId: string; taskId: string }) {
  const [logs, setLogs] = useState<{ stdout: string; stderr: string; run: ProviderRun | null } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.missions.taskLogs(missionId, taskId)
      .then((data) => { if (!cancelled) setLogs(data); })
      .catch((e) => { if (!cancelled) setError((e as Error).message); });
    return () => { cancelled = true; };
  }, [missionId, taskId]);

  if (error) {
    return <div className="card" style={{ borderColor: "var(--red)" }}><p className="muted">Failed to load logs: {error}</p></div>;
  }

  if (!logs) {
    return <div className="card"><p className="muted">Loading task logs…</p></div>;
  }

  const run = logs.run;
  const hasOutput = logs.stdout || logs.stderr;

  return (
    <div className="card" data-testid="task-log-panel">
      <div className="row spread" style={{ marginBottom: 8 }}>
        <h3>Task Log</h3>
        {run && (
          <div className="row" style={{ gap: 12 }}>
            <span className="muted" style={{ fontSize: 11 }}>
              Provider: <span className="mono">{run.provider}</span>
            </span>
            <span className="muted" style={{ fontSize: 11 }}>
              Role: <span className="mono">{run.role}</span>
            </span>
            {run.started_at && (
              <span className="muted" style={{ fontSize: 11 }}>
                Started: {new Date(run.started_at).toLocaleTimeString()}
              </span>
            )}
            {run.finished_at && run.started_at && (
              <span className="muted" style={{ fontSize: 11 }}>
                Duration: {((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000).toFixed(1)}s
              </span>
            )}
          </div>
        )}
      </div>
      {!hasOutput && <p className="muted">No log output yet.</p>}
      {logs.stdout && (
        <div style={{ marginBottom: 8 }}>
          <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>stdout</div>
          <pre className="terminal" style={{ height: 160, fontSize: 11 }}>{logs.stdout}</pre>
        </div>
      )}
      {logs.stderr && (
        <div>
          <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>stderr</div>
          <pre className="terminal" style={{ height: 100, fontSize: 11 }}>{logs.stderr}</pre>
        </div>
      )}
    </div>
  );
}
