import { Badge } from "./Badge";
import type { TaskRecord, TaskDependency } from "../lib/types";

export interface DagGraphProps {
  tasks: TaskRecord[];
  dependencies: TaskDependency[];
  activeTaskId?: string | null;
  onTaskClick?: (taskId: string) => void;
}

export function DagGraph({ tasks, dependencies, activeTaskId, onTaskClick }: DagGraphProps) {
  if (!tasks.length) {
    return <p className="muted">No tasks in this mission yet.</p>;
  }

  // Build adjacency maps
  const depsByTask: Record<string, string[]> = {};
  for (const d of dependencies) {
    if (!depsByTask[d.to_task_id]) depsByTask[d.to_task_id] = [];
    depsByTask[d.to_task_id].push(d.from_task_id);
  }

  // Topological levels (simple BFS)
  const inDegree: Record<string, number> = {};
  for (const t of tasks) inDegree[t.id] = 0;
  for (const d of dependencies) {
    inDegree[d.to_task_id] = (inDegree[d.to_task_id] || 0) + 1;
  }
  const levels: string[][] = [];
  const placed = new Set<string>();
  let queue = tasks.filter((t) => !inDegree[t.id]).map((t) => t.id);
  while (queue.length) {
    levels.push([...queue]);
    for (const id of queue) placed.add(id);
    const next: string[] = [];
    for (const id of queue) {
      for (const d of dependencies) {
        if (d.from_task_id === id && !placed.has(d.to_task_id)) {
          const remaining = (depsByTask[d.to_task_id] || []).filter((dep) => !placed.has(dep));
          if (remaining.length === 0 && !next.includes(d.to_task_id)) {
            next.push(d.to_task_id);
          }
        }
      }
    }
    queue = next;
  }
  // Any remaining (cycles) go in last level
  const remaining = tasks.filter((t) => !placed.has(t.id)).map((t) => t.id);
  if (remaining.length) levels.push(remaining);

  const taskMap = Object.fromEntries(tasks.map((t) => [t.id, t]));

  return (
    <div className="dag-graph" style={{ overflowX: "auto", padding: "8px 0" }}>
      <p className="muted">Read left to right. Tasks in the same column have no dependency on each other; provider capacity and file locks may still limit parallel execution.</p>
      <div style={{ display: "flex", gap: 24, alignItems: "flex-start" }}>
        {levels.map((level, li) => (
          <div key={li} style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 180 }}>
            <h4 className="muted">{li === 0 ? "Independent work" : `Dependency stage ${li + 1}`}</h4>
            {level.map((tid) => {
              const t = taskMap[tid];
              if (!t) return null;
              const isActive = activeTaskId === tid;
              return (
                <button
                  type="button"
                  aria-label={`${t.title || t.role}: ${t.status}`}
                  aria-pressed={isActive}
                  key={tid}
                  className={`card dag-node ${isActive ? "active" : ""}`}
                  style={{
                    padding: 10,
                    textAlign: "left",
                    maxWidth: 280,
                    cursor: onTaskClick ? "pointer" : "default",
                    borderColor: isActive ? "var(--accent)" : undefined,
                    boxShadow: isActive ? "0 0 0 2px color-mix(in srgb, var(--accent) 30%, transparent)" : undefined,
                  }}
                  onClick={() => onTaskClick?.(tid)}
                  data-testid={`dag-node-${tid}`}
                >
                  <div className="row spread" style={{ marginBottom: 6 }}>
                    <Badge value={t.status} pulse={["RUNNING", "CLAIMED", "WAITING_FOR_PROVIDER"].includes(t.status)} />
                  </div>
                  <div style={{ fontWeight: 600, fontSize: 12.5, marginBottom: 4 }}>{t.title || t.role}</div>
                  <div className="muted">{(depsByTask[tid] || []).length ? `Depends on: ${depsByTask[tid].map(id => taskMap[id]?.title || id).join(", ")}` : "No upstream dependencies"}</div>
                  {t.status === "STALE" && <p>Upstream work changed. Retry with current inputs.</p>}
                  {t.input_sha && <p className="muted">Input pinned · {t.result_sha ? "result recorded" : "awaiting result"}</p>}
                  {t.assigned_provider && (
                    <div className="muted" style={{ fontSize: 11 }}>
                      Provider: <span className="mono">{t.assigned_provider}</span>
                    </div>
                  )}
                  {t.workspace_scope && t.workspace_scope !== "[]" && (
                    <div className="muted" style={{ fontSize: 11 }}>
                      Scope: <span className="mono">{t.workspace_scope}</span>
                    </div>
                  )}
                  {t.blocking_issue && (
                    <div style={{ fontSize: 11, color: "var(--red)", marginTop: 4 }}>{t.blocking_issue}</div>
                  )}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      {dependencies.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-faint)" }}>
          {dependencies.map((d) => (
            <span key={`${d.from_task_id}-${d.to_task_id}`} className="mono" style={{ marginRight: 12 }}>
              {taskMap[d.from_task_id]?.title || d.from_task_id} → {taskMap[d.to_task_id]?.title || d.to_task_id}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
