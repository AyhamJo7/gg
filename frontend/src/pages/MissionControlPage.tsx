import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useElapsed, usePolling } from "../lib/hooks";
import { useMissionEvents } from "../lib/ws";
import { Badge } from "../components/Badge";
import { DagGraph } from "../components/DagGraph";
import { GateCard } from "../components/GateCard";
import { GitPanel } from "../components/GitPanel";
import { IntegrationPanel } from "../components/IntegrationPanel";
import { ProviderStrip } from "../components/ProviderStrip";
import { ReviewFindingsPanel } from "../components/ReviewFindingsPanel";
import { TaskLogPanel } from "../components/TaskLogPanel";
import { TaskPanel } from "../components/TaskPanel";
import { Terminal } from "../components/Terminal";
import { WorkflowTimeline } from "../components/WorkflowTimeline";
import type { Mission } from "../lib/types";

function MissionHeader({ mission }: { mission: Mission }) {
  const active = !["COMPLETED", "FAILED", "CANCELLED", "PAUSED", "UNVERIFIED"].includes(mission.status);
  const elapsed = useElapsed(mission.created_at, active);
  return (
    <div className="row spread">
      <div>
        <h1>{mission.title}</h1>
        <div className="muted" style={{ fontSize: 12.5 }}>{mission.task.slice(0, 160)}</div>
        <div className="row" style={{ marginTop: 4, gap: 8 }}>
          <Badge value={mission.status} pulse={active} />
          {mission.scheduling_mode === "PARALLEL_SAFE" && <Badge value="PARALLEL" />}
          <span className="mono muted">{elapsed}</span>
        </div>
      </div>
    </div>
  );
}

export function MissionControlPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const { data: missions, refresh: refreshMissions } = usePolling(() => api.missions.list(), 3000);
  const { data: providers } = usePolling(() => api.providers.list(), 5000);
  const mission = missions?.find((m) => m.id === selectedId) ?? missions?.[0] ?? null;
  const { data: detail, refresh: refreshDetail } = usePolling(
    () => (mission ? api.missions.get(mission.id) : Promise.resolve(null)),
    2500,
    [mission?.id],
  );
  const { data: dag } = usePolling(
    () => (mission ? api.missions.dag(mission.id) : Promise.resolve(null)),
    3000,
    [mission?.id],
  );
  const { data: git } = usePolling(
    () => (mission ? api.projects.git(mission.project_id) : Promise.resolve(null)),
    4000,
    [mission?.project_id],
  );
  const { terminal } = useMissionEvents(mission?.id ?? null);

  const isParallel = mission?.scheduling_mode === "PARALLEL_SAFE";

  const act = async (action: "pause" | "resume" | "cancel") => {
    if (!mission) return;
    await api.missions.action(mission.id, action);
    refreshMissions();
    refreshDetail();
  };

  const refreshAll = () => {
    refreshMissions();
    refreshDetail();
  };

  if (missions && missions.length === 0) {
    return (
      <div className="empty-state">
        <h1>No missions yet</h1>
        <p className="muted">Create a project, then launch your first mission.</p>
        <Link to="/new"><button className="primary">New Mission</button></Link>
      </div>
    );
  }

  const openGate = detail?.gates.find((g) => g.status === "open") ?? null;
  const integration = detail?.integrations?.[0] ?? null;

  return (
    <div className="stack">
      <div className="row" style={{ gap: 12 }}>
        <select
          value={mission?.id ?? ""}
          onChange={(e) => setSelectedId(e.target.value)}
          style={{ maxWidth: 320 }}
          data-testid="mission-select"
        >
          {missions?.map((m) => (
            <option key={m.id} value={m.id}>
              {m.title} — {m.status}
            </option>
          ))}
        </select>
        {mission && !["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED"].includes(mission.status) && (
          <>
            {mission.status === "PAUSED" ? (
              <button onClick={() => act("resume")}>▶ Resume</button>
            ) : (
              <button onClick={() => act("pause")}>⏸ Pause</button>
            )}
            <button className="danger" onClick={() => act("cancel")}>✕ Cancel</button>
          </>
        )}
        {mission && ["FAILED", "CANCELLED", "UNVERIFIED"].includes(mission.status) && (
          <button
            onClick={async () => {
              const newM = await api.missions.retry(mission.id);
              setSelectedId(newM.id);
              refreshMissions();
            }}
          >
            🔄 Retry
          </button>
        )}
      </div>

      {mission && (
        <>
          <MissionHeader mission={mission} />
          {openGate && <GateCard gate={openGate} missionId={mission.id} onResolved={refreshDetail} />}

          <div className="card">
            <ProviderStrip providers={providers ?? []} active={mission.current_provider} />
            <div style={{ marginTop: 12 }}>
              <WorkflowTimeline currentPhase={mission.current_phase} status={mission.status} />
            </div>
          </div>

          {mission.blocking_issue && (
            <div className="card" style={{ borderColor: "var(--orange)" }}>
              <h3>Blocking issue</h3>
              <pre className="muted" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{mission.blocking_issue}</pre>
            </div>
          )}

          {detail?.degraded_review && detail.latest_review && (
            <div className="card" style={{ borderColor: "var(--orange)" }} data-testid="self-review-warning">
              <div className="row">
                <Badge value="SELF-REVIEW" />
                <strong>DEGRADED REVIEW — independent reviewer unavailable</strong>
              </div>
              <p className="muted" style={{ margin: "6px 0 0" }}>
                Reviewer <span className="mono">{detail.latest_review.review_provider}</span> also performed the
                implementation. {detail.latest_review.degradation_reason}
              </p>
            </div>
          )}

          {isParallel && dag && (
            <div className="card">
              <h3>Task DAG</h3>
              <DagGraph
                tasks={dag.tasks}
                dependencies={dag.dependencies}
                activeTaskId={selectedTaskId}
                onTaskClick={(id) => setSelectedTaskId(id === selectedTaskId ? null : id)}
              />
            </div>
          )}

          <div className="grid-2">
            <div className="stack">
              {isParallel && detail ? (
                <>
                  <h3>Tasks ({detail.tasks.length})</h3>
                  {detail.tasks.map((t) => (
                    <TaskPanel key={t.id} task={t} missionId={mission.id} onRefresh={refreshAll} />
                  ))}
                  {selectedTaskId && (
                    <TaskLogPanel missionId={mission.id} taskId={selectedTaskId} />
                  )}
                </>
              ) : (
                <div className="card">
                  <h3>Current task</h3>
                  {detail?.tasks.length ? (
                    <>
                      <div className="row">
                        <Badge value={detail.tasks[detail.tasks.length - 1].role} />
                        <Badge value={detail.tasks[detail.tasks.length - 1].status} />
                      </div>
                      <p className="muted" style={{ marginTop: 8 }}>
                        {detail.tasks[detail.tasks.length - 1].summary || "Running…"}
                      </p>
                    </>
                  ) : (
                    <p className="muted">No task started yet</p>
                  )}
                </div>
              )}
              <IntegrationPanel integration={integration} />
            </div>
            <div className="stack">
              <ReviewFindingsPanel findings={detail?.findings ?? []} />
              <div className="card">
                <h3>Git ledger</h3>
                <GitPanel git={git ?? null} />
              </div>
            </div>
          </div>

          <div className="card">
            <h3>Live provider output</h3>
            <Terminal lines={terminal} />
          </div>

          {detail?.latest_handoff && (
            <div className="card">
              <h3>Latest handoff</h3>
              <pre className="muted" style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 260, overflow: "auto" }}>
                {detail.latest_handoff}
              </pre>
            </div>
          )}

          {detail && detail.runs.length > 0 && (
            <div className="card">
              <h3>Provider runs</h3>
              <table>
                <thead>
                  <tr><th>Provider</th><th>Role</th><th>Outcome</th><th>Exit</th><th>Started</th></tr>
                </thead>
                <tbody>
                  {detail.runs.map((r) => (
                    <tr key={r.id}>
                      <td style={{ textTransform: "capitalize" }}>{r.provider}</td>
                      <td>{r.role}</td>
                      <td><Badge value={r.failure_class === "NONE" ? "COMPLETED" : r.failure_class} /></td>
                      <td className="mono">{r.exit_code ?? "—"}</td>
                      <td className="muted">{new Date(r.started_at).toLocaleTimeString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
