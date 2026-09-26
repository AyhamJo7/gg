import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { useElapsed, usePolling } from "../lib/hooks";
import { useMissionEvents } from "../lib/ws";
import { Badge } from "../components/Badge";
import { ConflictCard } from "../components/ConflictCard";
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
import { TERMINAL_TASK_STATUSES, type Mission } from "../lib/types";
import { RunInspector } from "../components/RunInspector";
import { operatorLabel, TERMINAL_MISSION_STATES } from "../lib/operator";
import { MissionVerdictBadge, ReviewTrustCard } from "../components/MissionVerdict";

function MissionHeader({ mission }: { mission: Mission }) {
  const active = !["COMPLETED", "FAILED", "CANCELLED", "PAUSED", "UNVERIFIED"].includes(mission.status);
  const elapsed = useElapsed(mission.created_at, active);
  return (
    <div className="row spread">
      <div>
        <h1>{mission.title}</h1>
        <details><summary>Mission objective</summary><p className="muted">{mission.task}</p></details>
        <div className="row" style={{ marginTop: 4, gap: 8 }}>
          <Badge value={mission.status} pulse={active} />
          {TERMINAL_MISSION_STATES.has(mission.status) && <MissionVerdictBadge mission={mission} />}
          {mission.scheduling_mode === "PARALLEL_SAFE" && <Badge value="PARALLEL" />}
          <span className="mono muted">{active ? `Elapsed ${elapsed}` : mission.finished_at ? `Finished ${new Date(mission.finished_at).toLocaleString()}` : "Not running"}</span>
        </div>
      </div>
    </div>
  );
}

export function MissionControlPage() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("mission");
  const setSelectedId = (id: string) => { setParams({ mission: id }); setSelectedTaskId(null); setInspectedRunId(null); };
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [inspectedRunId, setInspectedRunId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { data: missions, error: loadError, refresh: refreshMissions } = usePolling(() => api.missions.list(), 3000);
  const { data: providers } = usePolling(() => api.providers.list(), 5000);
  const mission = selectedId ? missions?.find((m) => m.id === selectedId) ?? null : missions?.[0] ?? null;
  useEffect(() => { setSelectedTaskId(null); setInspectedRunId(null); setActionError(null); }, [mission?.id]);
  const { data: detail, error: detailError, refresh: refreshDetail } = usePolling(
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
    if (action === "cancel" && !window.confirm("Cancel this mission? Completed work and evidence are preserved.")) return;
    setBusy(true); setActionError(null);
    try { await api.missions.action(mission.id, action); refreshMissions(); refreshDetail(); }
    catch (e) { setActionError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
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
      {(loadError || detailError || actionError) && <div role="alert" className="notice error">{actionError || "Updates unavailable; displayed information may be stale."}<button onClick={refreshAll}>Refresh</button></div>}
      {selectedId && missions && !mission && <p role="alert">This mission was not found. Select another mission; GG will not silently open a different one.</p>}
      <div className="row" style={{ gap: 12 }}>
        <select
          aria-label="Select mission"
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
              <button disabled={busy} onClick={() => act("resume")}>▶ Resume</button>
            ) : (
              <button disabled={busy} onClick={() => act("pause")}>⏸ Pause</button>
            )}
            <button disabled={busy} className="danger" onClick={() => act("cancel")}>✕ Cancel</button>
          </>
        )}
        {mission && ["FAILED", "CANCELLED", "UNVERIFIED"].includes(mission.status) && (
          <button
            onClick={async () => {
              setBusy(true); setActionError(null);
              try { const newM = await api.missions.retry(mission.id); setSelectedId(newM.id); refreshMissions(); }
              catch (e) { setActionError(e instanceof Error ? e.message : String(e)); }
              finally { setBusy(false); }
            }}
            disabled={busy}
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

          {mission.blocking_issue && !openGate && (
            <div className="card" style={{ borderColor: "var(--orange)" }}>
              <h3>Blocking issue</h3>
              <pre className="muted" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{mission.blocking_issue}</pre>
            </div>
          )}

          {detail?.trust?.review ? (
            <ReviewTrustCard review={detail.trust.review} />
          ) : detail?.degraded_review && detail.latest_review && (
            <div className="card attention-card" data-testid="review-trust-warning">
              <strong>Review not certified as independent</strong>
              <p className="muted">
                Reviewer <span className="mono">{detail.latest_review.review_provider}</span>. Recorded reason:{" "}
                {detail.latest_review.degradation_reason || "No reason was recorded."}
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
                  {selectedTaskId &&
                    (() => {
                      const selectedTask = detail.tasks.find((t) => t.id === selectedTaskId);
                      const live = !!selectedTask && !TERMINAL_TASK_STATUSES.includes(selectedTask.status);
                      return <TaskLogPanel missionId={mission.id} taskId={selectedTaskId} active={live} />;
                    })()}
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
              {isParallel && <IntegrationPanel integration={integration} />}
              {integration?.status === "MERGE_CONFLICT" && mission && (
                <ConflictCard integration={integration} missionId={mission.id} onResumed={refreshAll} />
              )}
            </div>
            <div className="stack">
              <ReviewFindingsPanel findings={detail?.findings ?? []} />
              <div className="card">
                <h3>Git ledger</h3>
                <GitPanel git={git ?? null} />
              </div>
            </div>
          </div>

          <details className="card">
            <summary>Live provider output & logs</summary>
            <Terminal lines={terminal} />
          </details>

          {detail?.latest_handoff && (
            <details className="card">
              <summary>Latest handoff</summary>
              <pre className="muted" style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 260, overflow: "auto" }}>
                {detail.latest_handoff}
              </pre>
            </details>
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
                      <td style={{ textTransform: "capitalize" }}>
                        <button
                          className="link"
                          onClick={() => setInspectedRunId(r.id)}
                          title="Inspect invocation"
                        >
                          {r.provider}
                        </button>
                      </td>
                      <td>{r.role}</td>
                      <td title={r.run_status || r.failure_class}>{operatorLabel(r.run_status || (r.failure_class === "NONE" ? "COMPLETED" : r.failure_class))}</td>
                      <td className="mono">{r.exit_code ?? "—"}</td>
                      <td className="muted">{new Date(r.started_at).toLocaleTimeString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {inspectedRunId && (
                <div style={{ marginTop: 12 }}>
                  <RunInspector key={inspectedRunId} runId={inspectedRunId} onClose={() => setInspectedRunId(null)} />
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
