import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge } from "../components/Badge";
import { EvidencePanel } from "../components/EvidencePanel";
import { RepairCyclePanel } from "../components/RepairCyclePanel";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import type { ProductGateRow, ProductProjectDetail } from "../lib/types";

type Tab = "plan" | "roadmap" | "execution" | "gates" | "delivery";

export function LifecycleDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: project, refresh } = usePolling(() => api.lifecycle.get(id!), 3000, [id]);
  const { data: missions } = usePolling(() => api.missions.list(), 5000);
  const [tab, setTab] = useState<Tab>("plan");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [planEdit, setPlanEdit] = useState<string | null>(null);
  const [reviseReason, setReviseReason] = useState("");
  const [resolutions, setResolutions] = useState<Record<string, string>>({});

  if (!project) return <div className="muted">Loading product project…</div>;

  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    setBusy(true);
    try {
      await fn();
      refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const missionById = (mid: string | null) => missions?.find((m) => m.id === mid);
  const openGates = project.gates.filter((g) => g.status === "open");
  const state = project.state;

  return (
    <div style={{ maxWidth: 980 }}>
      <Link to="/lifecycle" className="muted">← All product projects</Link>
      <div className="row" style={{ gap: 12, marginTop: 8, flexWrap: "wrap" }}>
        <h1 style={{ margin: 0 }}>{project.name}</h1>
        <Badge value={state} />
        {project.acceptance_state && <Badge value={project.acceptance_state} />}
      </div>
      {project.blocking_reason && <p style={{ color: "var(--red)" }}>{project.blocking_reason}</p>}
      {error && <p style={{ color: "var(--red)" }}>{error}</p>}
      <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
        {(state === "DRAFT" || state === "PLAN_READY" || state === "BLOCKED") && (
          <button onClick={() => run(() => api.lifecycle.plan(project.id))} disabled={busy} data-testid="lifecycle-plan">
            Generate Plan
          </button>
        )}
        {(state === "PLAN_READY" || state === "DRAFT") && project.plan && (
          <button className="primary" onClick={() => run(() => api.lifecycle.start(project.id))} disabled={busy} data-testid="lifecycle-start">
            Start Project
          </button>
        )}
        {!["DELIVERED", "FAILED", "CANCELLED", "DRAFT", "PLAN_READY"].includes(state) && (
          <>
            <button onClick={() => run(() => api.lifecycle.advance(project.id))} disabled={busy}>Advance Now</button>
            <button onClick={() => run(() => api.lifecycle.pause(project.id))} disabled={busy}>Pause</button>
          </>
        )}
        {!["DELIVERED", "FAILED", "CANCELLED"].includes(state) && (
          <button className="danger" onClick={() => run(() => api.lifecycle.cancel(project.id))} disabled={busy}>
            Cancel
          </button>
        )}
        {["FINAL_ACCEPTANCE", "BLOCKED", "WAITING_FOR_HUMAN"].includes(state) && (
          <button onClick={() => run(() => api.lifecycle.acceptance(project.id))} disabled={busy} data-testid="lifecycle-accept">
            Run Acceptance
          </button>
        )}
      </div>

      <div className="row" style={{ gap: 4, marginTop: 16 }}>
        {(["plan", "roadmap", "execution", "gates", "delivery"] as Tab[]).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={tab === t ? "primary" : undefined} data-testid={`tab-${t}`}>
            {t === "gates" && openGates.length > 0 ? `Gates (${openGates.length})` : t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {tab === "plan" && <PlanTab project={project} planEdit={planEdit} setPlanEdit={setPlanEdit} reviseReason={reviseReason} setReviseReason={setReviseReason} run={run} />}
      {tab === "roadmap" && <RoadmapTab project={project} run={run} />}
      {tab === "execution" && (
        <div style={{ marginTop: 12 }}>
          {project.phases.length === 0 && <p className="muted">No phases yet — generate a plan and start the project.</p>}
          {project.phases.map((p) => {
            const m = missionById(p.mission_id);
            return (
              <div className="card" key={p.id} style={{ marginTop: 8 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>{p.title}</strong>
                  <Badge value={p.status} />
                </div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {p.mission_id ? (
                    <>
                      mission <span className="mono">{p.mission_id}</span>
                      {m && (
                        <>
                          {" "}· {m.status} · phase {m.current_phase ?? "—"} · provider {m.current_provider ?? "—"}
                        </>
                      )}{" "}
                      <Link to="/">open in Mission Control</Link>
                    </>
                  ) : (
                    "no mission yet"
                  )}
                </div>
                {p.blocking_issue && <div style={{ fontSize: 12, color: "var(--red)" }}>{p.blocking_issue}</div>}
              </div>
            );
          })}
        </div>
      )}
      {tab === "gates" && (
        <div style={{ marginTop: 12 }}>
          {project.gates.length === 0 && <p className="muted">No human gates. GG handles everything itself so far.</p>}
          {project.gates.map((g) => (
            <GateView
              key={g.id}
              gate={g}
              project={project}
              resolution={resolutions[g.id] ?? ""}
              setResolution={(v) => setResolutions((s) => ({ ...s, [g.id]: v }))}
              run={run}
            />
          ))}
        </div>
      )}
      {tab === "delivery" && <DeliveryTab project={project} run={run} />}
    </div>
  );
}

function PlanTab({
  project, planEdit, setPlanEdit, reviseReason, setReviseReason, run,
}: {
  project: ProductProjectDetail;
  planEdit: string | null;
  setPlanEdit: (v: string | null) => void;
  reviseReason: string;
  setReviseReason: (v: string) => void;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const plan = project.plan;
  if (!plan) return <p className="muted" style={{ marginTop: 12 }}>No plan yet — press Generate Plan.</p>;
  const arch = plan.architecture as Record<string, unknown>;
  return (
    <div style={{ marginTop: 12 }}>
      <div className="card">
        <h3>{plan.product_name} <span className="muted">· revision {project.plan_revision}</span></h3>
        <p>{plan.goal}</p>
        <p className="muted">Users: {plan.users}</p>
        <h4>Stack</h4>
        <ul>
          {["frontend", "backend", "database", "auth", "deployment"].map((k) => (
            <li key={k}><strong>{k}:</strong> {String(arch[k] ?? "—")}</li>
          ))}
        </ul>
        {(arch.decisions as Array<{ area: string; choice: string; rationale: string }> | undefined)?.map((d, i) => (
          <p key={i} style={{ fontSize: 13 }}><strong>{d.area}:</strong> {d.choice} — {d.rationale}</p>
        ))}
      </div>
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Requirements ({plan.requirements.length})</h4>
        {plan.requirements.map((r) => (
          <div key={r.id} style={{ marginBottom: 8 }}>
            <strong>{r.id}</strong> · {r.title} <span className="muted">({r.kind})</span>
            <ul>
              {r.acceptance.map((a) => (
                <li key={a.id} style={{ fontSize: 13 }}>{a.id}: {a.description} <span className="muted">— verify: {a.verify || "—"}</span></li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Roadmap ({plan.phases.length} phases)</h4>
        {plan.phases.map((p) => (
          <div key={p.key} style={{ marginBottom: 8 }}>
            <strong>{p.key}</strong> · {p.title} <span className="muted">(effort {p.effort})</span>
            <div className="muted" style={{ fontSize: 12 }}>
              depends: {p.depends_on.join(", ") || "—"} · covers: {p.requirement_ids.join(", ")}
              {p.human_prerequisites.length > 0 && ` · needs human: ${p.human_prerequisites.join(", ")}`}
            </div>
          </div>
        ))}
      </div>
      {plan.external_prerequisites.length > 0 && (
        <div className="card" style={{ marginTop: 8 }}>
          <h4>External prerequisites</h4>
          {plan.external_prerequisites.map((e) => (
            <p key={e.key} style={{ fontSize: 13 }}><strong>{e.title}</strong> — {e.human_action}</p>
          ))}
        </div>
      )}
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Edit plan (new auditable revision)</h4>
        {planEdit === null ? (
          <button onClick={() => setPlanEdit(JSON.stringify(plan, null, 2))}>Edit JSON</button>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            <textarea rows={14} className="mono" value={planEdit} onChange={(e) => setPlanEdit(e.target.value)} data-testid="plan-json" />
            <input placeholder="Reason for this revision (required)" value={reviseReason} onChange={(e) => setReviseReason(e.target.value)} data-testid="plan-reason" />
            <div className="row">
              <button
                className="primary"
                onClick={() => run(async () => {
                  await api.lifecycle.revisePlan(project.id, JSON.parse(planEdit), reviseReason);
                  setPlanEdit(null);
                  setReviseReason("");
                })}
                data-testid="plan-save"
              >
                Save as new revision
              </button>
              <button onClick={() => setPlanEdit(null)}>Cancel</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function RoadmapTab({ project, run }: { project: ProductProjectDetail; run: (fn: () => Promise<unknown>) => Promise<void> }) {
  if (project.phases.length === 0) return <p className="muted" style={{ marginTop: 12 }}>No phases yet.</p>;
  const evByReq = Object.fromEntries(project.evidence.map((e) => [e.requirement_id, e.status]));
  return (
    <div style={{ marginTop: 12 }}>
      {project.phases.map((p, i) => (
        <div className="card" key={p.id} style={{ marginTop: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong>{i + 1}. {p.title}</strong>
            <Badge value={p.status} />
          </div>
          <div className="muted" style={{ fontSize: 12 }}>{p.goal}</div>
          <div className="muted" style={{ fontSize: 12 }}>
            depends: {p.depends_on.join(", ") || "—"} · attempts: {p.attempts}
            {p.acceptance_json.map((a) => ` · [${a.id}]`).join("")}
          </div>
          {p.blocking_issue && <div style={{ fontSize: 12, color: "var(--red)" }}>{p.blocking_issue}</div>}
          {["FAILED", "BLOCKED"].includes(p.status) && (
            <button style={{ marginTop: 6 }} onClick={() => run(() => api.lifecycle.retryPhase(project.id, p.phase_key))}>
              Retry phase
            </button>
          )}
        </div>
      ))}
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Requirement coverage</h4>
        {project.plan?.requirements.map((r) => (
          <div key={r.id} style={{ fontSize: 13 }}>
            <strong>{r.id}</strong> {r.title} — {evByReq[r.id] ?? "PENDING"}
          </div>
        ))}
      </div>
    </div>
  );
}

function GateView({
  gate, project, resolution, setResolution, run,
}: {
  gate: ProductGateRow;
  project: ProductProjectDetail;
  resolution: string;
  setResolution: (v: string) => void;
  run: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const isSecret = gate.gate_type === "secret";
  return (
    <div className="card" style={{ marginTop: 8 }} data-testid="gate-card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>{gate.title}</strong>
        <Badge value={gate.status} />
      </div>
      <dl style={{ fontSize: 13, display: "grid", gap: 4 }}>
        <div><dt><strong>What is required?</strong></dt><dd>{gate.what_required || "—"}</dd></div>
        <div><dt><strong>Why?</strong></dt><dd>{gate.why_required || "—"}</dd></div>
        <div><dt><strong>Blocked:</strong></dt><dd>{gate.blocked_ref}</dd></div>
        <div><dt><strong>Already completed:</strong></dt><dd>{gate.completed_so_far || "—"}</dd></div>
        <div><dt><strong>Your action:</strong></dt><dd>{gate.human_action}</dd></div>
        <div><dt><strong>Where:</strong></dt><dd>{gate.where_to_provide || "—"}</dd></div>
        {isSecret && gate.required_vars.length > 0 && (
          <div>
            <dt><strong>Required variables (names only — never paste values here):</strong></dt>
            <dd className="mono">{gate.required_vars.join(", ")}</dd>
            <dd className="muted">
              Add them to <span className="mono">{project.target_repo_path}/.env</span> in your own editor,
              then confirm below. GG only checks the names exist; values never enter the database.
            </dd>
          </div>
        )}
        <div><dt><strong>After resolve:</strong></dt><dd>{gate.after_resolve || "roadmap resumes"}</dd></div>
        {gate.resolution && <div><dt><strong>Resolution:</strong></dt><dd>{gate.resolution}</dd></div>}
      </dl>
      {gate.status === "open" && (
        <div className="row" style={{ gap: 8 }}>
          {!isSecret && (
            <input
              placeholder="Resolution note (required)"
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              style={{ flex: 1 }}
              data-testid="gate-resolution"
            />
          )}
          <button
            className="primary"
            onClick={() => run(() => api.lifecycle.resolveGate(project.id, gate.id, isSecret ? "configured" : resolution))}
            data-testid="gate-resolve"
          >
            {isSecret ? "I configured it — validate & resume" : "Resolve & resume"}
          </button>
        </div>
      )}
    </div>
  );
}

function DeliveryTab({ project, run }: { project: ProductProjectDetail; run: (fn: () => Promise<unknown>) => Promise<void> }) {
  const [waiveReason, setWaiveReason] = useState("");
  const report = project.delivery_report as {
    product_name?: string; git_sha?: string; repo_path?: string;
    stack?: Record<string, string>; decisions?: Array<{ area: string; choice: string; rationale: string }>;
    requirements?: Array<{ id: string; title: string; status: string;
      criteria?: Array<{ id: string; status: string; command: string; exit_code: number | null; sha: string }> }>;
    phases?: Array<{ key: string; title: string; status: string; mission_id: string | null }>;
    review_notes?: string[]; recent_toolchain?: string[];
    human_gates?: Array<{ title: string; status: string; resolution: string | null }>;
    env_vars?: string[]; run_instructions?: string;
    waivers?: Array<{ target: string; reason: string; actor: string; plan_revision: number }>;
  };
  if (project.state !== "DELIVERED" || !report?.git_sha) {
    const failedCriteria = (project.criterion_results ?? []).filter((c) =>
      ["FAILED", "UNVERIFIED"].includes(c.status),
    );
    const waivedIds = new Set((project.waivers ?? []).map((w) => `${w.target_kind}:${w.target_id}`));
    const pendingWaivers = failedCriteria.filter((c) => !waivedIds.has(`criterion:${c.criterion_id}`));
    return (
      <div style={{ marginTop: 12 }}>
        <div className="card">
          <p className="muted">
            No delivery yet. Acceptance state: <strong>{project.acceptance_state || "PENDING"}</strong>.
            {project.blocking_reason && <> — {project.blocking_reason}</>}
          </p>
        </div>
        <EvidencePanel projectId={project.id} />
        <RepairCyclePanel projectId={project.id} refresh={run} />
        {pendingWaivers.length > 0 && (
          <div className="card" style={{ marginTop: 8 }} data-testid="waiver-panel">
            <h4>Failed criteria — authorize a waiver to proceed without them</h4>
            {pendingWaivers.map((c) => (
              <div key={c.criterion_id} style={{ fontSize: 13, marginBottom: 4 }}>
                <span className="mono">{c.criterion_id}</span> — {c.status}
                <span className="muted"> (exit {c.exit_code ?? "—"})</span>
              </div>
            ))}
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <input
                placeholder="Waiver reason (required, recorded with plan revision)"
                value={waiveReason}
                onChange={(e) => setWaiveReason(e.target.value)}
                style={{ flex: 1 }}
                data-testid="waiver-reason"
              />
              <button
                className="primary"
                disabled={!waiveReason.trim()}
                onClick={() =>
                  run(async () => {
                    for (const c of pendingWaivers) {
                      await api.lifecycle.waive(project.id, "criterion", c.criterion_id, waiveReason);
                    }
                    setWaiveReason("");
                  })
                }
                data-testid="waiver-submit"
              >
                Record waiver
              </button>
            </div>
          </div>
        )}
        {(project.waivers ?? []).length > 0 && (
          <div className="card" style={{ marginTop: 8 }}>
            <h4>Recorded waivers</h4>
            {project.waivers.map((w) => (
              <div key={w.id} style={{ fontSize: 12 }}>
                <span className="mono">{w.target_kind}:{w.target_id}</span> — {w.reason}
                <span className="muted"> (by {w.actor}, plan rev {w.plan_revision})</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }
  return (
    <div style={{ marginTop: 12 }}>
      <div className="card">
        <h3>{report.product_name} — delivered</h3>
        <p>Exact commit: <span className="mono" data-testid="delivery-sha">{report.git_sha}</span></p>
        <p className="muted mono" style={{ fontSize: 12 }}>{report.repo_path}</p>
        <EvidencePanel projectId={project.id} />
        <p style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{report.run_instructions}</p>
        {report.env_vars && report.env_vars.length > 0 && (
          <p style={{ fontSize: 13 }}>Environment variables (names): <span className="mono">{report.env_vars.join(", ")}</span></p>
        )}
      </div>
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Requirements</h4>
        {report.requirements?.map((r) => (
          <div key={r.id} style={{ fontSize: 13, marginBottom: 6 }}>
            <strong>{r.id}</strong> {r.title} — {r.status}
            {r.criteria?.map((c) => (
              <div key={c.id} className="mono muted" style={{ fontSize: 11, marginLeft: 12 }}>
                {c.id}: {c.status} (exit {c.exit_code ?? "—"}) {c.sha.slice(0, 8)} — {c.command.slice(0, 90)}
              </div>
            ))}
          </div>
        ))}
      </div>
      {(report.waivers ?? []).length > 0 && (
        <div className="card" style={{ marginTop: 8 }}>
          <h4>Authorized waivers</h4>
          {report.waivers?.map((w, i) => (
            <div key={i} style={{ fontSize: 12 }}>
              <span className="mono">{w.target}</span> — {w.reason}
              <span className="muted"> (by {w.actor}, plan rev {w.plan_revision})</span>
            </div>
          ))}
        </div>
      )}
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Review notes</h4>
        {(!report.review_notes || report.review_notes.length === 0) && <p className="muted">No review findings recorded.</p>}
        {report.review_notes?.map((n, i) => <p key={i} style={{ fontSize: 12 }}>{n}</p>)}
      </div>
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Toolchain evidence (recent)</h4>
        {report.recent_toolchain?.map((t, i) => <div key={i} className="mono muted" style={{ fontSize: 11 }}>{t}</div>)}
      </div>
    </div>
  );
}
