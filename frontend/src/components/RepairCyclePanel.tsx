import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { RepairCycle } from "../lib/types";

function short(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 8) : "—";
}

function StatusChip({ status }: { status: string }) {
  const color =
    status === "SUCCEEDED"
      ? "green"
      : status === "EXHAUSTED" || status === "FAILED" || status === "BLOCKED"
        ? "red"
        : "yellow";
  return (
    <span className={`badge ${color}`} style={{ marginLeft: 6 }}>
      {status}
    </span>
  );
}

export function RepairCyclePanel({
  projectId,
  refresh,
}: {
  projectId: string;
  refresh: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [cycles, setCycles] = useState<RepairCycle[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.lifecycle
      .repairCycles(projectId)
      .then((d) => {
        if (!cancelled) setCycles(d.cycles ?? []);
      })
      .catch(() => {
        if (!cancelled) setError("repair cycles unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (error) return <p className="muted">{error}</p>;
  if (cycles.length === 0) return null;

  return (
    <div className="card" style={{ marginTop: 8 }} data-testid="repair-panel">
      <h4>Autonomous repair</h4>
      {cycles.map((c) => (
        <div key={c.id} style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 13 }}>
            <span className="mono">{c.id.slice(0, 12)}</span> · {c.trigger_type} {c.target_criterion_id ?? c.target_finding_id ?? ""}
            <StatusChip status={c.status} />
          </div>
          <p className="mono" style={{ fontSize: 12 }}>
            Attempt {c.attempts_used}/{c.max_attempts} · base {short(c.trigger_sha)} · {c.classification}
          </p>
          {(c.attempts ?? []).map((a) => (
            <div key={a.id} className="mono" style={{ fontSize: 12, marginLeft: 12 }}>
              #{a.attempt_number} {a.provider || "—"} {short(a.base_sha)}→{short(a.result_sha)} · {a.outcome}
              {a.review_reviewer ? ` · reviewed by ${a.review_reviewer} (${a.review_outcome})` : ""}
              {a.recheck_outcome ? ` · recheck ${a.recheck_outcome}` : ""}
            </div>
          ))}
          {c.stop_reason && <p className="muted" style={{ fontSize: 12 }}>{c.stop_reason}</p>}
          {["CREATED", "CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING", "WAITING_FOR_PROVIDER"].includes(c.status) && (
            <button
              onClick={() => refresh(async () => api.lifecycle.cancelRepairCycle(projectId, c.id))}
              data-testid={`cancel-repair-${c.id}`}
            >
              Cancel repair
            </button>
          )}
        </div>
      ))}
      <p className="muted" style={{ fontSize: 12 }}>
        Bounded repair of proven implementation defects only — reviewed independently and rechecked
        against the exact repaired SHA. Success returns a candidate to acceptance; it never delivers directly.
      </p>
    </div>
  );
}
