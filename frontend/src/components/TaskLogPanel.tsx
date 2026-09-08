import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { TaskLogsResponse } from "../lib/types";

const POLL_MS = 2500;
const TAIL_BYTES = 65536;

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function TaskLogPanel({
  missionId,
  taskId,
  active,
  pollMs = POLL_MS,
}: {
  missionId: string;
  taskId: string;
  active: boolean;
  pollMs?: number;
}) {
  const [logs, setLogs] = useState<TaskLogsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Generation token: identifies the current (mission, task) selection so
  // late responses from a previous task can never overwrite current logs.
  const genRef = useRef(0);

  useEffect(() => {
    const gen = ++genRef.current;
    setLogs(null);
    setError(null);
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;
    let disposed = false;

    const fresh = () => !disposed && genRef.current === gen;

    const load = async () => {
      controller?.abort();
      controller = new AbortController();
      try {
        const data = await api.missions.taskLogs(missionId, taskId, TAIL_BYTES, controller.signal);
        if (!fresh()) return;
        setLogs(data);
        setError(null);
      } catch (e) {
        if (!fresh()) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError((e as Error).message);
      }
      // Chain the next poll only after this one settles: at most one
      // in-flight request per selected task, no timer accumulation, and a
      // slow response can never overlap its successor. Errors also
      // reschedule while active, so transient failures reconnect.
      if (fresh() && active) {
        timer = setTimeout(() => {
          timer = null;
          void load();
        }, pollMs);
      }
    };

    void load();
    return () => {
      disposed = true;
      controller?.abort();
      if (timer) clearTimeout(timer);
    };
  }, [missionId, taskId, active, pollMs]);

  if (error && !logs) {
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
        <h3>
          Task Log{" "}
          <span className="muted" style={{ fontSize: 11 }} data-testid="task-log-live">
            {active ? "● live" : "■ final"}
          </span>
        </h3>
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
      {(logs.stdout_truncated || logs.stderr_truncated) && (
        <p className="muted" style={{ fontSize: 11 }}>
          Showing last {formatBytes(TAIL_BYTES)} of {formatBytes(Math.max(logs.stdout_size, logs.stderr_size))} — older output truncated.
        </p>
      )}
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
