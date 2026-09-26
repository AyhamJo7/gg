import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import {
  FINISHED_PRODUCT_STATES, TERMINAL_MISSION_STATES, missionHref, missionVerdict, productAttention,
} from "../lib/operator";
import type { Mission, ProductProjectSummary, RepairCycle } from "../lib/types";
import { Badge } from "../components/Badge";
import { MissionVerdictBadge } from "../components/MissionVerdict";

// Only active products need repair polling; bounded fan-out keeps history cheap.
const REPAIR_DETAIL_LIMIT = 20;
const RECENT_OUTCOME_LIMIT = 8;
const ACTIVE_MISSION_LIMIT = 8;

async function snapshot() {
  const [products, missions, providers] = await Promise.all([
    api.lifecycle.list(), api.missions.list(), api.providers.list(),
  ]);
  const active = products.filter(p => !FINISHED_PRODUCT_STATES.has(p.state));
  const repairs: Record<string, RepairCycle[]> = {};
  let unavailable = active.length > REPAIR_DETAIL_LIMIT;
  await Promise.all(active.slice(0, REPAIR_DETAIL_LIMIT).map(async p => {
    try { repairs[p.id] = (await api.lifecycle.repairCycles(p.id)).cycles; }
    catch { unavailable = true; }
  }));
  return { products, missions, providers, repairs, unavailable };
}

type Outcome =
  | { kind: "product"; at: string; product: ProductProjectSummary }
  | { kind: "mission"; at: string; mission: Mission };

function productSummary(p: ProductProjectSummary, activeRepair: boolean): string {
  if (p.blocking_reason) return p.blocking_reason;
  if (activeRepair) return "GG will repair, independently review, and recheck before acceptance.";
  if (p.state === "DRAFT") return "Generate a plan to get started.";
  if (p.state === "PLAN_READY") return "Review the plan, then start the build.";
  const total = Object.values(p.phase_counts).reduce((a, b) => a + b, 0);
  return `${total} phases · ${p.phase_counts.COMPLETED ?? 0} completed`;
}

function MissionRow({ m }: { m: Mission }) {
  const verdict = missionVerdict(m);
  const terminal = TERMINAL_MISSION_STATES.has(m.status);
  const summary = m.blocking_issue || (terminal ? null : `Mission · ${m.current_provider || "No provider running"}`);
  return <Link className="operator-row" to={missionHref(m.id)}>
    <div>
      <strong>{m.title}</strong>
      {summary && <p className="muted clamp" title={summary}>{summary}</p>}
      {terminal && <p><MissionVerdictBadge mission={m} /></p>}
    </div>
    <span className="operator-state">{terminal ? "Inspect" : verdict.label} →</span>
  </Link>;
}

export function OverviewPage() {
  const { data, error, refresh } = usePolling(snapshot, 5000);
  const productStatus = (p: ProductProjectSummary) => productAttention(p, data?.repairs[p.id]);
  const attention = data?.products.filter(p => productStatus(p).needsAttention) ?? [];
  const active = data?.products.filter(p => !FINISHED_PRODUCT_STATES.has(p.state) &&
    !productStatus(p).needsAttention) ?? [];
  const missionAttention = data?.missions.filter(m => missionVerdict(m).needsAttention) ?? [];
  const activeMissions = data?.missions.filter(m => !TERMINAL_MISSION_STATES.has(m.status) &&
    !missionVerdict(m).needsAttention).slice(0, ACTIVE_MISSION_LIMIT) ?? [];
  const outcomes: Outcome[] = data ? [
    ...data.products.filter(p => FINISHED_PRODUCT_STATES.has(p.state) && !productStatus(p).needsAttention)
      .map(p => ({ kind: "product" as const, at: p.updated_at, product: p })),
    ...data.missions.filter(m => TERMINAL_MISSION_STATES.has(m.status) && !missionVerdict(m).needsAttention)
      .map(m => ({ kind: "mission" as const, at: m.finished_at || m.updated_at, mission: m })),
  ].sort((a, b) => b.at.localeCompare(a.at)).slice(0, RECENT_OUTCOME_LIMIT) : [];

  const productRow = (p: ProductProjectSummary) => {
    const status = productStatus(p);
    return <Link key={p.id} className="operator-row" to={`/lifecycle/${p.id}`}>
      <div><strong>{p.name}</strong><p className="muted">Product · {productSummary(p, !!status.activeRepair)}</p></div>
      <span className="operator-state">{status.label} →</span>
    </Link>;
  };
  const nothingWaiting = !attention.length && !missionAttention.length;

  return <div className="stack operator-page">
    <header className="page-header"><div><h1>Overview</h1><p className="muted">Your builds, decisions, and recent deliveries.</p></div>
      <div className="row"><Link className="btn" to="/new">Work on a repository</Link>
        <Link className="btn primary" to="/lifecycle?create=1">Build a product</Link></div></header>
    {error && <div role="alert" className="notice error">Updates unavailable. Displayed information may be stale. <button onClick={refresh}>Retry</button><details><summary>Error details</summary>{error}</details></div>}
    {!data && !error && <p role="status">Loading your workspace…</p>}
    {data && <>
      {data.unavailable && <p role="status" className="notice">Some repair details are unavailable or outside the first {REPAIR_DETAIL_LIMIT} active products. Open a product for current repair status.</p>}
      <div className="operator-columns">
        <div className="stack">
          <section className="card" aria-labelledby="attention-heading">
            <h2 id="attention-heading">Needs your attention {!nothingWaiting && <span className="count">{attention.length + missionAttention.length}</span>}</h2>
            {attention.map(productRow)}
            {missionAttention.map(m => <MissionRow key={m.id} m={m} />)}
            {nothingWaiting && <p className="muted">Nothing is waiting on you. Finished work with open findings or an uncertified review would appear here.</p>}
            {missionAttention.length > 0 && attention.length > 0 && <p className="muted">Product phases run as missions, so a product stop may also appear as a mission stop.</p>}
          </section>
          <section className="card"><h2>In progress & ready to start</h2>
            {active.map(productRow)}
            {activeMissions.map(m => <MissionRow key={m.id} m={m} />)}
            {!active.length && !activeMissions.length && <p className="muted">Nothing is running. Start with an idea or a task in an existing repository.</p>}
          </section>
          <section className="card"><div className="row spread"><h2>Recent outcomes</h2><span className="row"><Link to="/missions">Missions</Link><Link to="/lifecycle">Products</Link></span></div>
            {outcomes.map(o => o.kind === "product" ? productRow(o.product) : <MissionRow key={o.mission.id} m={o.mission} />)}
            {!outcomes.length && <p className="muted">Deliveries, finished missions and stopped work will appear here. A completed mission is not necessarily a delivered product.</p>}
          </section>
        </div>
        <aside className="stack"><section className="card"><div className="row spread"><h2>Provider availability</h2><Link to="/providers">Manage</Link></div>
          {data.providers.map(p => <div className="operator-row" key={p.name}><div><strong>{p.name}</strong>{p.cooldown_until && <p className="muted">Eligible to retry after {new Date(p.cooldown_until).toLocaleTimeString()}</p>}</div><Badge value={p.state} /></div>)}
          <p className="muted">Availability is observed locally. Remaining subscription quota is not reported.</p></section>
          <section className="card"><h2>Two ways to work</h2><p><Link to="/lifecycle?create=1">Build a product</Link><br/><span className="muted">Idea, plan, build, acceptance, delivery.</span></p><p><Link to="/new">Run a mission</Link><br/><span className="muted">A scoped task in a repository you already own.</span></p></section>
        </aside>
      </div>
    </>}
  </div>;
}
