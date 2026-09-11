import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import type { DagTaskInput, DagDependencyInput } from "../lib/types";

interface DagTaskForm {
  id: string;
  title: string;
  description: string;
  role: string;
  preferred_providers: string;
  workspace_scope: string;
  priority: number;
}

function emptyTask(index: number): DagTaskForm {
  return {
    id: `task-${index + 1}`,
    title: "",
    description: "",
    role: "implementation",
    preferred_providers: "",
    workspace_scope: "",
    priority: 0,
  };
}

function validateDag(tasks: DagTaskForm[], deps: DagDependencyInput[]): string | null {
  const ids = new Set(tasks.map((t) => t.id));
  if (ids.size !== tasks.length) return "Duplicate task IDs are not allowed.";
  for (const t of tasks) {
    if (!t.id.trim() || !t.title.trim()) return "All tasks must have an ID and title.";
  }
  for (const d of deps) {
    if (!ids.has(d.from_task_id)) return `Dependency references unknown task: ${d.from_task_id}`;
    if (!ids.has(d.to_task_id)) return `Dependency references unknown task: ${d.to_task_id}`;
    if (d.from_task_id === d.to_task_id) return "Tasks cannot depend on themselves.";
  }
  // Cycle detection
  const adj: Record<string, string[]> = {};
  for (const t of tasks) adj[t.id] = [];
  for (const d of deps) adj[d.from_task_id].push(d.to_task_id);
  const visited = new Set<string>();
  const stack = new Set<string>();
  function dfs(node: string): boolean {
    visited.add(node);
    stack.add(node);
    for (const next of adj[node]) {
      if (!visited.has(next)) {
        if (dfs(next)) return true;
      } else if (stack.has(next)) {
        return true;
      }
    }
    stack.delete(node);
    return false;
  }
  for (const t of tasks) {
    if (!visited.has(t.id)) {
      if (dfs(t.id)) return "Dependency cycle detected.";
    }
  }
  return null;
}

export function NewMissionPage() {
  const navigate = useNavigate();
  const { data: projects } = usePolling(() => api.projects.list(), null);
  const { data: profiles } = usePolling(() => api.settings.profiles(), null);
  const [projectId, setProjectId] = useState("");
  const [title, setTitle] = useState("");
  const [task, setTask] = useState("");
  const [autonomy, setAutonomy] = useState("BALANCED");
  const [profile, setProfile] = useState("balanced");
  const [schedulingMode, setSchedulingMode] = useState("SEQUENTIAL");
  const [dagMode, setDagMode] = useState<"auto" | "manual">("auto");
  const [dagTasks, setDagTasks] = useState<DagTaskForm[]>([emptyTask(0), emptyTask(1)]);
  const [dagDeps, setDagDeps] = useState<DagDependencyInput[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateTask = (index: number, patch: Partial<DagTaskForm>) => {
    setDagTasks((prev) => prev.map((t, i) => (i === index ? { ...t, ...patch } : t)));
  };

  const addTask = () => {
    setDagTasks((prev) => [...prev, emptyTask(prev.length)]);
  };

  const removeTask = (index: number) => {
    setDagTasks((prev) => {
      const next = prev.filter((_, i) => i !== index);
      // Remove deps referencing removed task
      const removedId = prev[index]?.id;
      setDagDeps((deps) => deps.filter((d) => d.from_task_id !== removedId && d.to_task_id !== removedId));
      return next;
    });
  };

  const toggleDep = (from: string, to: string) => {
    setDagDeps((prev) => {
      const exists = prev.some((d) => d.from_task_id === from && d.to_task_id === to);
      if (exists) {
        return prev.filter((d) => !(d.from_task_id === from && d.to_task_id === to));
      }
      return [...prev, { from_task_id: from, to_task_id: to }];
    });
  };

  const submit = async () => {
    setError(null);
    if (schedulingMode === "PARALLEL_SAFE" && dagMode === "manual") {
      const validationError = validateDag(dagTasks, dagDeps);
      if (validationError) {
        setError(validationError);
        return;
      }
    }
    setBusy(true);
    try {
      const mission = await api.missions.create({
        project_id: projectId,
        title,
        task,
        autonomy,
        profile,
        scheduling_mode: schedulingMode,
        start: false,
      });

      if (schedulingMode === "PARALLEL_SAFE" && dagMode === "manual") {
        const tasksPayload: DagTaskInput[] = dagTasks.map((t) => ({
          id: t.id,
          role: t.role,
          title: t.title,
          description: t.description,
          preferred_providers: t.preferred_providers ? JSON.stringify(t.preferred_providers.split(",").map((s) => s.trim()).filter(Boolean)) : "[]",
          workspace_scope: t.workspace_scope ? JSON.stringify(t.workspace_scope.split(",").map((s) => s.trim()).filter(Boolean)) : "[]",
          priority: t.priority,
        }));
        await api.missions.updateDag(mission.id, { tasks: tasksPayload, dependencies: dagDeps });
      }

      await api.missions.action(mission.id, "start");
      navigate(`/missions?mission=${encodeURIComponent(mission.id)}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 720 }}>
      <h1>New Mission</h1>
      <p className="muted">One high-level engineering task. The orchestrator plans, executes, reviews, and verifies it across your AI subscriptions.</p>
      <div className="card stack" style={{ marginTop: 16 }}>
        <div className="field">
          <label>Repository</label>
          {projects && projects.length > 0 ? (
            <select aria-label="Repository" value={projectId} onChange={(e) => setProjectId(e.target.value)} data-testid="project-select">
              <option value="">Select a repository…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name} ({p.path})</option>
              ))}
            </select>
          ) : (
            <p className="muted">No repositories yet — add one on the Repositories page first.</p>
          )}
        </div>
        <div className="field">
          <label>Mission title</label>
          <input aria-label="Mission title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Build invoice management SaaS" />
        </div>
        <div className="field">
          <label>Task description</label>
          <textarea aria-label="Task description"
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
            <select aria-label="Autonomy level" value={autonomy} onChange={(e) => setAutonomy(e.target.value)}>
              <option value="SAFE">SAFE — confirm before implementation</option>
              <option value="BALANCED">BALANCED — gates on real decisions</option>
              <option value="AUTONOMOUS">AUTONOMOUS — only hard gates</option>
            </select>
          </div>
          <div className="field">
            <label>Execution profile</label>
            <select aria-label="Execution profile" value={profile} onChange={(e) => setProfile(e.target.value)}>
              <option value="balanced">Balanced</option>
              {Object.keys(profiles ?? {}).map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label>Scheduling mode</label>
          <select aria-label="Scheduling mode"
            value={schedulingMode}
            onChange={(e) => setSchedulingMode(e.target.value)}
            data-testid="scheduling-mode"
          >
            <option value="SEQUENTIAL">Sequential — one phase at a time</option>
            <option value="PARALLEL_SAFE">Parallel Safe — independent tasks run together</option>
          </select>
        </div>

        {schedulingMode === "PARALLEL_SAFE" && (
          <div className="field">
            <label>DAG creation</label>
            <div className="row" style={{ gap: 12 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                <input
                  type="radio"
                  name="dag-mode"
                  checked={dagMode === "auto"}
                  onChange={() => setDagMode("auto")}
                />
                Auto-plan (AI decomposes the mission)
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                <input
                  type="radio"
                  name="dag-mode"
                  checked={dagMode === "manual"}
                  onChange={() => setDagMode("manual")}
                />
                Manual DAG
              </label>
            </div>
          </div>
        )}

        {schedulingMode === "PARALLEL_SAFE" && dagMode === "manual" && (
          <div className="card" style={{ background: "var(--bg)" }}>
            <h3>Task DAG Editor</h3>
            <div className="stack" style={{ gap: 10 }}>
              {dagTasks.map((t, i) => (
                <div key={i} className="card" style={{ padding: 10 }} data-testid={`dag-task-${i}`}>
                  <div className="row spread" style={{ marginBottom: 8 }}>
                    <span className="mono faint">Task {i + 1}</span>
                    <button className="danger" style={{ padding: "2px 8px", fontSize: 11 }} onClick={() => removeTask(i)}>
                      Remove
                    </button>
                  </div>
                  <div className="grid-2">
                    <div className="field" style={{ marginBottom: 8 }}>
                      <label>ID</label>
                      <input aria-label={`Task ${i + 1} ID`} value={t.id} onChange={(e) => updateTask(i, { id: e.target.value })} />
                    </div>
                    <div className="field" style={{ marginBottom: 8 }}>
                      <label>Title</label>
                      <input aria-label={`Task ${i + 1} title`} value={t.title} onChange={(e) => updateTask(i, { title: e.target.value })} />
                    </div>
                  </div>
                  <div className="field" style={{ marginBottom: 8 }}>
                    <label>Description</label>
                    <input aria-label={`Task ${i + 1} description`} value={t.description} onChange={(e) => updateTask(i, { description: e.target.value })} />
                  </div>
                  <div className="grid-2">
                    <div className="field" style={{ marginBottom: 8 }}>
                      <label>Role</label>
                      <select aria-label={`Task ${i + 1} role`} value={t.role} onChange={(e) => updateTask(i, { role: e.target.value })}>
                        <option value="implementation">Implementation</option>
                        <option value="testing">Testing</option>
                        <option value="review">Review</option>
                        <option value="repair">Repair</option>
                      </select>
                    </div>
                    <div className="field" style={{ marginBottom: 8 }}>
                      <label>Priority</label>
                      <input aria-label={`Task ${i + 1} priority`} type="number" value={t.priority} onChange={(e) => updateTask(i, { priority: Number(e.target.value) })} />
                    </div>
                  </div>
                  <div className="grid-2">
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Preferred providers (comma-separated)</label>
                      <input aria-label={`Task ${i + 1} preferred providers`} value={t.preferred_providers} onChange={(e) => updateTask(i, { preferred_providers: e.target.value })} placeholder="agy, opencode" />
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Workspace scope (comma-separated)</label>
                      <input aria-label={`Task ${i + 1} workspace scope`} value={t.workspace_scope} onChange={(e) => updateTask(i, { workspace_scope: e.target.value })} placeholder="src/**, tests/**" />
                    </div>
                  </div>
                </div>
              ))}
            </div>
            <button style={{ marginTop: 10 }} onClick={addTask}>+ Add Task</button>

            {dagTasks.length > 1 && (
              <div style={{ marginTop: 14 }}>
                <h3 style={{ marginBottom: 8 }}>Dependencies</h3>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 8 }}>
                  {dagTasks.map((from) =>
                    dagTasks
                      .filter((to) => to.id !== from.id)
                      .map((to) => {
                        const checked = dagDeps.some((d) => d.from_task_id === from.id && d.to_task_id === to.id);
                        return (
                          <label key={`${from.id}-${to.id}`} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer" }}>
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => toggleDep(from.id, to.id)}
                            />
                            <span className="mono">{from.id}</span> → <span className="mono">{to.id}</span>
                          </label>
                        );
                      })
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {error && <p style={{ color: "var(--red)" }}>{error}</p>}
        <div>
          <button
            className="primary"
            disabled={busy || !projectId || !title || !task}
            onClick={submit}
            data-testid="launch-mission"
          >
            {busy ? "Launching…" : "▶ Launch Mission"}
          </button>
        </div>
      </div>
    </div>
  );
}
