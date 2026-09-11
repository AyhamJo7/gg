import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import { FINISHED_PRODUCT_STATES, missionHref, operatorLabel, productAttention } from "../lib/operator";
import type { RepairCycle } from "../lib/types";
import { Badge } from "../components/Badge";

// Only active products need repair polling; bounded fan-out keeps history cheap.
const REPAIR_DETAIL_LIMIT = 20;
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

export function OverviewPage() {
  const { data, error, refresh } = usePolling(snapshot, 5000);
  const attention = data?.products.filter(p => productAttention(p, data.repairs[p.id]).needsAttention) ?? [];
  const active = data?.products.filter(p => !FINISHED_PRODUCT_STATES.has(p.state) &&
    !productAttention(p, data.repairs[p.id]).needsAttention) ?? [];
  const missionAttention = data?.missions.filter(m =>
    ["WAITING_FOR_HUMAN", "BLOCKED", "FAILED", "UNVERIFIED"].includes(m.status)) ?? [];
  const recent = data?.products.filter(p => FINISHED_PRODUCT_STATES.has(p.state)).slice(0, 8) ?? [];
  const productRow = (p: NonNullable<typeof data>["products"][number]) => {
    const status = productAttention(p, data?.repairs[p.id]);
    return <Link key={p.id} className="operator-row" to={`/lifecycle/${p.id}`}>
      <div><strong>{p.name}</strong><p className="muted">{p.blocking_reason ||
        (status.activeRepair ? "GG will repair, independently review, and recheck before acceptance." :
          p.state === "DRAFT" ? "Generate a plan to get started." :
            p.state === "PLAN_READY" ? "Review the plan, then start the build." :
              `${Object.values(p.phase_counts).reduce((a, b) => a + b, 0)} phases · ${p.phase_counts.COMPLETED ?? 0} completed`)}
      </p></div><span className="operator-state">{status.label} →</span>
    </Link>;
  };
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
          <section className="card" aria-labelledby="attention-heading"><h2 id="attention-heading">Needs your attention</h2>
            {attention.map(productRow)}
            {!attention.length && <p className="muted">No product decisions are waiting. Mission-level stops are listed below.</p>}
            {missionAttention.length > 0 && <details open={!attention.length}><summary>{missionAttention.length} mission stops · may also appear in product decisions</summary>
              {missionAttention.map(m => <Link className="operator-row" key={m.id} to={missionHref(m.id)}><div><strong>{m.title}</strong><p className="muted">{m.blocking_issue || operatorLabel(m.status)}</p></div><span>Inspect →</span></Link>)}
            </details>}
          </section>
          <section className="card"><h2>In progress & ready to start</h2>{active.map(productRow)}
            {!active.length && <p className="muted">No active product builds. Start with an idea or a task in an existing repository.</p>}
            {data.missions.filter(m => !["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED", "WAITING_FOR_HUMAN", "BLOCKED"].includes(m.status)).slice(0, 8).map(m =>
              <Link key={m.id} className="operator-row" to={missionHref(m.id)}><div><strong>{m.title}</strong><p className="muted">Mission · {m.current_provider || "No provider running"}</p></div><span>{operatorLabel(m.status)} →</span></Link>)}
          </section>
          <section className="card"><div className="row spread"><h2>Recent outcomes</h2><Link to="/lifecycle">All products</Link></div>{recent.map(productRow)}
            {!recent.length && <p className="muted">Deliveries and stopped projects will appear here. Completed missions are not necessarily delivered products.</p>}</section>
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
