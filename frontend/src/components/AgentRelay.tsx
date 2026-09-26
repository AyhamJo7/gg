import { useState, type CSSProperties } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import {
  FLAG_SHORT, UNRECORDED_SENDER, entryLane, flagText, formatChars, formatDuration, laneColorIndex, relayLanes,
  runOutcomeLabel,
} from "../lib/relay";
import { operatorLabel } from "../lib/operator";
import type { MissionRelay, RelayFinding, RelayHandoff, RelayReview, RelayRun } from "../lib/types";

const LANE_PALETTE_SIZE = 6;
const ACTIVE_POLL_MS = 5000;
const SHA_DISPLAY_CHARS = 8;

function short(sha: string | null | undefined): string {
  return sha ? sha.slice(0, SHA_DISPLAY_CHARS) : "unknown";
}

function laneStyle(lanes: string[], name: string | null): CSSProperties {
  const index = name ? lanes.indexOf(name) : -1;
  return index < 0 ? {} : { gridColumn: `${index + 1}` };
}

function laneClass(lanes: string[], name: string): string {
  return `lane-${laneColorIndex(lanes, name, LANE_PALETTE_SIZE)}`;
}

type RowPlacement = { gridRow: string };

function RunStep({ run, lanes, row, thinLimit, onInspect }: {
  run: RelayRun; lanes: string[]; row: RowPlacement; thinLimit: number; onInspect?: (runId: string) => void;
}) {
  const ok = run.outcome === "SUCCEEDED";
  const flagged = run.flags.filter(f => f !== "CONTEXT_NOT_CAPTURED");
  return (
    <details className={`relay-step relay-run ${laneClass(lanes, run.provider)} ${flagged.length ? "flagged" : ""}`} style={{ ...laneStyle(lanes, run.provider), ...row }}>
      <summary>
        <span className="relay-step-head">
          <strong>{run.provider}</strong> · {run.role}
          <span className={`relay-outcome ${ok ? "ok" : run.outcome_source === "legacy" || run.outcome === "UNKNOWN" ? "" : "bad"}`}>
            {runOutcomeLabel(run.outcome)}{run.outcome_source === "legacy" ? " (legacy record)" : ""}
          </span>
        </span>
        <span className="relay-step-meta muted">
          {formatDuration(run.duration_ms)} · told {formatChars(run.context?.prompt_chars)}
          {run.changed_commit === true && " · committed"}
          {run.changed_commit === false && " · no commit"}
        </span>
        {run.flags.length > 0 && (
          <span className="relay-flags">
            {run.flags.map(f => (
              <span key={f} className={`relay-flag ${f === "CONTEXT_NOT_CAPTURED" ? "muted" : ""}`} title={flagText(f, run, thinLimit)}>
                {FLAG_SHORT[f]}
              </span>
            ))}
          </span>
        )}
      </summary>
      <div className="relay-step-body">
        {run.flags.map(f => <p key={f} className={f === "CONTEXT_NOT_CAPTURED" ? "muted" : "relay-flag-text"}>{flagText(f, run, thinLimit)}</p>)}
        <dl className="facts">
          <dt>What it was told</dt>
          <dd>
            {run.context
              ? <>{formatChars(run.context.prompt_chars)} in {run.context.block_count} blocks
                {run.context.estimated_prompt_tokens !== null && <> · ~{run.context.estimated_prompt_tokens.toLocaleString("en-US")} tokens (estimate)</>}</>
              : "not captured"}
          </dd>
          {run.context && run.context.warnings.length > 0 && <><dt>Compiler warnings</dt><dd className="mono">{run.context.warnings.join(", ")}</dd></>}
          <dt>Commit</dt><dd className="mono">{short(run.commit_before)} → {short(run.commit_after)}</dd>
          {run.model_observed && <><dt>Model</dt><dd className="mono">{run.model_observed}</dd></>}
          {run.exit_code !== null && <><dt>Exit</dt><dd className="mono">{run.exit_code}</dd></>}
        </dl>
        {run.summary && <pre className="relay-text">{run.summary}{run.summary_truncated ? "…" : ""}</pre>}
        {onInspect && <button className="link" onClick={() => onInspect(run.id)}>Open run inspector</button>}
      </div>
    </details>
  );
}

function HandoffStep({ handoff, lanes, row, missionId }: { handoff: RelayHandoff; lanes: string[]; row: RowPlacement; missionId: string }) {
  const [full, setFull] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const sender = handoff.from_provider ?? UNRECORDED_SENDER;
  const a = lanes.indexOf(sender);
  const b = handoff.to_provider ? lanes.indexOf(handoff.to_provider) : a;
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  const direction = a <= b ? "forward" : "backward";
  const load = async () => {
    setLoading(true); setError(null);
    try { setFull((await api.missions.handoff(missionId, handoff.id)).content); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  };
  return (
    <details className={`relay-step relay-handoff ${direction}`} style={{ ...(lo >= 0 ? { gridColumn: `${lo + 1} / ${hi + 2}` } : {}), ...row }}>
      <summary>
        <span className="relay-arrow" aria-hidden>{direction === "forward" ? "→" : "←"}</span>
        <span>
          <strong>{sender}</strong> handed off to <strong>{handoff.to_provider ?? "an unrecorded provider"}</strong>
          {handoff.role && <> for {handoff.role}</>}
          <span className="muted"> · {formatChars(handoff.content_chars)}</span>
        </span>
      </summary>
      <div className="relay-step-body">
        {!handoff.from_provider && <p className="muted">The sender was not recorded; GG compiled this handoff.</p>}
        <pre className="relay-text">{full ?? handoff.preview}{!full && handoff.preview_truncated ? "…" : ""}</pre>
        {handoff.preview_truncated && !full && (
          <button onClick={load} disabled={loading}>{loading ? "Loading…" : `Show all ${formatChars(handoff.content_chars)}`}</button>
        )}
        {error && <p role="alert" className="notice error">Could not load the full handoff. {error}</p>}
      </div>
    </details>
  );
}

function ReviewStep({ review, lanes, row }: { review: RelayReview; lanes: string[]; row: RowPlacement }) {
  return (
    <div className={`relay-step relay-review ${review.reviewer ? laneClass(lanes, review.reviewer) : ""} ${review.independent ? "" : "flagged"}`} style={{ ...laneStyle(lanes, entryLane(review)), ...row }}>
      <span className="relay-step-head">
        <strong>{review.reviewer || "unknown reviewer"}</strong> · review recorded
        <span className={`relay-outcome ${review.independent ? "ok" : "bad"}`}>
          {review.independent ? "independent" : review.reviewer_in_writer_set ? "self-review" : "not certified"}
        </span>
      </span>
      <span className="relay-step-meta muted">
        writers: {review.writer_set.length ? review.writer_set.join(", ") : "not recorded"} · range {short(review.reviewed_base_sha)} → {short(review.reviewed_head_sha)}
      </span>
      {!review.independent && review.degradation_reason && <span className="relay-flag-text">{review.degradation_reason}</span>}
    </div>
  );
}

const STATUS_ORDER = ["open", "repair_attempted", "resolved"];

function FindingLineage({ findings }: { findings: RelayFinding[] }) {
  if (!findings.length) return <p className="muted">No review findings were recorded for this mission.</p>;
  const sorted = [...findings].sort((a, b) => {
    const ai = STATUS_ORDER.indexOf(a.status), bi = STATUS_ORDER.indexOf(b.status);
    return (ai < 0 ? STATUS_ORDER.length : ai) - (bi < 0 ? STATUS_ORDER.length : bi);
  });
  return (
    <ol className="lineage">
      {sorted.map(f => (
        <li key={f.id} className={`lineage-item status-${f.status}`}>
          <div className="row">
            <span className="mono">{f.severity}</span>
            <strong>{f.file || f.category || "general"}</strong>
            <span className="muted">{operatorLabel(f.status)}</span>
          </div>
          <p>{f.description}{f.text_truncated ? "…" : ""}</p>
          <p className="muted lineage-path">
            raised at {short(f.origin_sha)}
            {f.inherited_from_mission_id && <> · inherited from mission {f.inherited_from_mission_id.slice(0, SHA_DISPLAY_CHARS)}</>}
            {f.status === "repair_attempted" && <> → repair claimed, <strong>not verified</strong></>}
            {f.status === "resolved" && <> → verified fixed at {short(f.resolved_sha)}{f.verified_by && <> by {f.verified_by}</>}</>}
          </p>
        </li>
      ))}
    </ol>
  );
}

export function AgentRelayView({ relay, onInspectRun }: { relay: MissionRelay; onInspectRun?: (runId: string) => void }) {
  const lanes = relayLanes(relay);
  if (!relay.timeline.length) return <p className="muted">No provider has run for this mission yet.</p>;
  return (
    <div className="stack">
      <div className="relay-totals">
        {relay.providers.map(p => (
          <div key={p.provider} className={`relay-total ${laneClass(lanes, p.provider)}`}>
            <strong>{p.provider}</strong>
            <span className="muted">{p.roles.join(", ")}</span>
            <span>{p.succeeded}/{p.runs} succeeded{p.in_flight ? ` · ${p.in_flight} in flight` : ""}</span>
            <span className="muted">
              {formatDuration(p.known_duration_ms)}{p.unknown_duration_runs ? ` + ${p.unknown_duration_runs} run(s) of unknown length` : ""}
            </span>
          </div>
        ))}
      </div>
      {relay.inherited_findings_available === false && <p className="notice">Findings inherited from earlier attempts could not be loaded.</p>}
      {relay.runs_truncated && <p className="notice">Showing the first {relay.limits.run_limit} runs only.</p>}
      <div className="relay-lanes" style={{ gridTemplateColumns: `repeat(${lanes.length}, minmax(180px, 1fr))` }} aria-label="Agent relay timeline">
        {lanes.map((lane, i) => (
          <div key={`guide-${lane}`} className="relay-lane-guide" aria-hidden style={{ gridColumn: `${i + 1}`, gridRow: `1 / ${relay.timeline.length + 2}` }} />
        ))}
        {lanes.map((lane, i) => (
          <div key={lane} className={`relay-lane-head ${lane === UNRECORDED_SENDER ? "" : laneClass(lanes, lane)}`} style={{ gridColumn: `${i + 1}`, gridRow: "1" }}>{lane}</div>
        ))}
        {relay.timeline.map((entry, row) => {
          const place = { gridRow: `${row + 2}` };
          if (entry.kind === "run") return <RunStep key={`run-${entry.id}`} run={entry} lanes={lanes} row={place} thinLimit={relay.limits.thin_evidence_block_chars} onInspect={onInspectRun} />;
          if (entry.kind === "handoff") return <HandoffStep key={`handoff-${entry.id}`} handoff={entry} lanes={lanes} row={place} missionId={relay.mission_id} />;
          return <ReviewStep key={`review-${entry.id ?? row}`} review={entry} lanes={lanes} row={place} />;
        })}
      </div>
      <section>
        <h4>Finding lineage</h4>
        <FindingLineage findings={relay.findings} />
      </section>
    </div>
  );
}

export function AgentRelay({ missionId, active, onInspectRun }: { missionId: string; active: boolean; onInspectRun?: (runId: string) => void }) {
  const { data, error, refresh } = usePolling(() => api.missions.relay(missionId), active ? ACTIVE_POLL_MS : null, [missionId]);
  if (error && !data) return <div role="alert" className="notice error">The agent relay is unavailable. <button onClick={refresh}>Retry</button></div>;
  if (!data) return <p role="status" className="muted">Loading agent relay…</p>;
  return <>
    {error && <p role="status" className="notice">Relay updates unavailable; showing the last loaded state.</p>}
    <AgentRelayView relay={data} onInspectRun={onInspectRun} />
  </>;
}
