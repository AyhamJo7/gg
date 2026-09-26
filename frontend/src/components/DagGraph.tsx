import { useLayoutEffect, useRef, useState } from "react";
import { Badge } from "./Badge";
import type { TaskRecord, TaskDependency } from "../lib/types";

export interface DagGraphProps {
  tasks: TaskRecord[];
  dependencies: TaskDependency[];
  activeTaskId?: string | null;
  onTaskClick?: (taskId: string) => void;
}

interface Edge { key: string; d: string; from: string; to: string }

const EDGE_CURVE = 0.5;

/** Measure node boxes and draw dependency curves (right edge → left edge). */
function useDagEdges(dependencies: TaskDependency[], layoutKey: string) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const measure = () => {
      const origin = container.getBoundingClientRect();
      const box = (id: string) => container.querySelector<HTMLElement>(`[data-node-id="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      const next: Edge[] = [];
      const boxes = new Map<string, DOMRect>();
      for (const dep of dependencies) {
        for (const id of [dep.from_task_id, dep.to_task_id]) {
          const r = boxes.has(id) ? boxes.get(id) : box(id);
          if (r) boxes.set(id, r);
        }
      }
      const drawable = dependencies.filter(d => boxes.has(d.from_task_id) && boxes.has(d.to_task_id));
      // Spread edges over "ports" along each card side so they never merge.
      const port = (id: string, side: "in" | "out", other: string) => {
        const peers = drawable
          .filter(d => (side === "in" ? d.to_task_id : d.from_task_id) === id)
          .map(d => (side === "in" ? d.from_task_id : d.to_task_id))
          .sort((x, y) => boxes.get(x)!.top - boxes.get(y)!.top);
        const r = boxes.get(id)!;
        return r.top + (r.height * (peers.indexOf(other) + 1)) / (peers.length + 1) - origin.top;
      };
      for (const dep of drawable) {
        const a = boxes.get(dep.from_task_id)!;
        const b = boxes.get(dep.to_task_id)!;
        const x1 = a.right - origin.left, y1 = port(dep.from_task_id, "out", dep.to_task_id);
        const x2 = b.left - origin.left, y2 = port(dep.to_task_id, "in", dep.from_task_id);
        const dx = Math.max(24, (x2 - x1) * EDGE_CURVE);
        next.push({ key: `${dep.from_task_id}->${dep.to_task_id}`, from: dep.from_task_id, to: dep.to_task_id,
          d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}` });
      }
      setEdges(next);
      setSize({ w: container.scrollWidth, h: container.scrollHeight });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, [dependencies, layoutKey]);
  return { containerRef, edges, size };
}

export function DagGraph({ tasks, dependencies, activeTaskId, onTaskClick }: DagGraphProps) {
  const layoutKey = tasks.map(t => `${t.id}:${t.status}`).join("|");
  const { containerRef, edges, size } = useDagEdges(dependencies, layoutKey);
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
      <div ref={containerRef} className="dag-canvas">
        <svg className="dag-edges" width={size.w} height={size.h} aria-hidden="true" data-testid="dag-edges">
          <defs>
            <marker id="dag-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L8 4 L0 8 z" fill="currentColor" />
            </marker>
          </defs>
          {edges.map(e => {
            const upstreamDone = taskMap[e.from]?.status === "COMPLETED";
            const focused = activeTaskId === e.from || activeTaskId === e.to;
            return <path key={e.key} d={e.d} className={`dag-edge ${upstreamDone ? "done" : ""} ${focused ? "focused" : ""}`} markerEnd="url(#dag-arrow)" />;
          })}
        </svg>
        <div className="dag-columns">
        {levels.map((level, li) => (
          <div key={li} className="dag-column">
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
                  data-node-id={tid}
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
      </div>
      {dependencies.length > 0 && (
        <details className="dag-dependency-list">
          <summary>Dependency list ({dependencies.length})</summary>
          <ul>
            {dependencies.map((d) => (
              <li key={`${d.from_task_id}-${d.to_task_id}`} className="mono">
                {taskMap[d.from_task_id]?.title || d.from_task_id} → {taskMap[d.to_task_id]?.title || d.to_task_id}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
