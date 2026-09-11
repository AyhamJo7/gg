import { Link } from "react-router-dom";
import type { Mission, ProductProjectDetail } from "../lib/types";
import { FINISHED_PRODUCT_STATES, missionHref, operatorLabel } from "../lib/operator";
import { EvidencePanel } from "./EvidencePanel";
import { RepairCyclePanel } from "./RepairCyclePanel";

export function ProductOverview({ project, headline, missions, onAttention, run }: {
  project: ProductProjectDetail; headline: string; missions: Mission[]; onAttention: () => void;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const gates = FINISHED_PRODUCT_STATES.has(project.state) ? [] : project.gates.filter(g => g.status === "open");
  const current = project.phases.filter(p => !["COMPLETED", "PENDING", "CANCELLED"].includes(p.status));
  const paused = !!project.paused;
  return <div className="operator-columns section-gap">
    <div className="stack">
      <section className="card"><h2>{headline}</h2>
        <p>{project.blocking_reason || (project.state === "DELIVERED" ? "The delivery report and exact evidence are available in Delivery & evidence." :
          project.state === "DRAFT" ? "Describe your idea, generate a plan, then review it before building." :
            project.state === "PLAN_READY" ? "Review the proposed requirements and architecture. Start Project approves execution of this plan." :
              "GG advances the roadmap automatically. Active work and any required decisions appear here.")}</p>
        {paused && <p className="muted">No new work will launch until you resume.</p>}
        {project.phases.some(p => missions.find(m => m.id === p.mission_id)?.status === "PAUSED") && <p className="notice">A phase mission is paused. Resuming the project does not resume that mission: open it below and choose Resume when ready.</p>}
        {gates.length > 0 && <div className="notice"><strong>{gates.length} decision{gates.length === 1 ? "" : "s"} need you</strong>
          <ul>{gates.map(g => <li key={g.id}>{g.title}</li>)}</ul><button className="primary" onClick={onAttention}>Review required actions</button></div>}
        {current.map(p => {
          const mission = missions.find(m => m.id === p.mission_id);
          return <div className="operator-row" key={p.id}><div><strong>{p.title}</strong><p className="muted">{operatorLabel(p.status)} · {mission?.current_provider || "No provider assigned"}</p></div>
            {p.mission_id && <Link to={missionHref(p.mission_id)}>Open mission →</Link>}</div>;
        })}
      </section>
      <RepairCyclePanel projectId={project.id} refresh={run} />
      <section className="card"><h2>Build roadmap</h2>
        {!project.phases.length && <p className="muted">Phases appear after you start an approved plan.</p>}
        <ol className="phase-list">{project.phases.map(p => <li key={p.id}>
          <div><strong>{p.title}</strong><p className="muted">{p.goal}</p></div><span>{operatorLabel(p.status)}</span>
        </li>)}</ol>
      </section>
    </div>
    <aside className="stack"><section><h2>Verification & trust</h2><EvidencePanel projectId={project.id} /></section></aside>
  </div>;
}
