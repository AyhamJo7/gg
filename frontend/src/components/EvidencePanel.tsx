import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { ArtifactEvidence } from "../lib/types";

function short(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 8) : "—";
}

function StateChip({ state }: { state: string }) {
  const color = state === "VALID" ? "green" : state === "FAILED" || state === "STALE" ? "red" : "yellow";
  return (
    <span className={`badge ${color}`} style={{ marginLeft: 6 }}>
      {state}
    </span>
  );
}

export function EvidencePanel({ projectId }: { projectId: string }) {
  const [evidence, setEvidence] = useState<ArtifactEvidence | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.lifecycle
      .evidence(projectId)
      .then((d) => {
        if (!cancelled) setEvidence(d);
      })
      .catch(() => {
        if (!cancelled) setError("evidence unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (error) return <p className="muted">{error}</p>;
  if (!evidence) return <p className="muted">Loading evidence…</p>;

  const review = evidence.review as { state: string; phases?: Array<Record<string, unknown>> };
  const criteria = evidence.criteria as {
    total: number; passed: number; stale: number; failed: number; missing: number; waived: number;
  };
  const writers = (evidence.writers ?? []) as Array<{
    actor_type: string; provider: string | null; run_id: string | null; result_sha: string;
  }>;
  const attempts = (evidence.phase_attempts ?? []) as Array<Record<string, unknown>>;

  return (
    <div data-testid="evidence-panel" style={{ marginTop: 8 }}>
      <div className="card">
        <h4>
          Delivery readiness: {evidence.delivery_ready ? "READY" : "NOT READY"}
          <span className={`badge ${evidence.delivery_ready ? "green" : "red"}`} style={{ marginLeft: 6 }}>
            {evidence.delivery_ready ? "READY" : "BLOCKED"}
          </span>
        </h4>
        <p className="mono" style={{ fontSize: 12 }}>
          Candidate {short(evidence.candidate_sha)} · plan rev {evidence.plan_revision}
          {" · "}writers {evidence.writers_complete ? "complete" : "INCOMPLETE"}
        </p>
        {(evidence.blocking_reasons ?? []).length > 0 && (
          <ul style={{ fontSize: 13 }}>
            {(evidence.blocking_reasons ?? []).map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="card" style={{ marginTop: 8 }}>
        <h4>Writers</h4>
        {writers.length === 0 && <p className="muted">No attributed writes in range.</p>}
        {writers.map((w, i) => (
          <div key={i} className="mono" style={{ fontSize: 12 }}>
            {w.actor_type}
            {w.provider ? ` · ${w.provider}` : ""} · {short(w.result_sha)}
            {w.run_id ? <span className="muted"> · run {w.run_id.slice(0, 12)} (see Runs)</span> : null}
          </div>
        ))}
        <h4 style={{ marginTop: 8 }}>
          Review <StateChip state={review.state} />
        </h4>
        {(review.phases ?? []).map((p, i) => (
          <div key={i} style={{ fontSize: 12 }}>
            <span className="mono">{String(p.phase_id)}</span> {short(p.candidate_sha as string)}
            <StateChip state={String(p.state)} />
            {p.reviewer ? <span className="muted"> by {String(p.reviewer)}</span> : null}
            {p.detail ? <span className="muted"> — {String(p.detail)}</span> : null}
          </div>
        ))}
        <h4 style={{ marginTop: 8 }}>
          Verification <StateChip state={(evidence.verification as { state: string }).state} /> · Criteria{" "}
          {criteria.passed}/{criteria.total} passed
          {criteria.stale > 0 && <span className="muted"> ({criteria.stale} stale)</span>}
          {criteria.failed > 0 && <span className="muted"> ({criteria.failed} failed)</span>}
          {criteria.missing > 0 && <span className="muted"> ({criteria.missing} missing)</span>}
          {criteria.waived > 0 && <span className="muted"> ({criteria.waived} waived)</span>} · Fresh checkout{" "}
          <StateChip state={(evidence.fresh_checkout as { state: string }).state} />
        </h4>
      </div>
      {attempts.length > 0 && (
        <div className="card" style={{ marginTop: 8 }}>
          <h4>Phase attempts</h4>
          {attempts.map((a, i) => (
            <div key={i} className="mono" style={{ fontSize: 12 }}>
              {String(a.phase_id)} #{String(a.attempt_number)} {String(a.trigger)} {String(a.status)}{" "}
              {short(a.result_sha as string)}
            </div>
          ))}
        </div>
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        States reflect the exact candidate SHA above — never a historical pass. Raw prompts and
        secrets are never displayed.
      </p>
    </div>
  );
}
