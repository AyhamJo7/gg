import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { RunDetail } from "../lib/types";
import { EstimateValue, UsageValue } from "./UsageValue";
import { Badge } from "./Badge";

export function RunInspector({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [contextDetail, setContextDetail] = useState<Record<string, unknown> | null>(null);
  const [showContext, setShowContext] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .runs()
      .get(runId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  useEffect(() => {
    if (!showContext || contextDetail) return;
    let cancelled = false;
    api
      .runs()
      .context(runId)
      .then((d) => {
        if (!cancelled) setContextDetail(d);
      })
      .catch(() => {
        if (!cancelled) setContextDetail({ error: "context unavailable" });
      });
    return () => {
      cancelled = true;
    };
  }, [showContext, contextDetail, runId]);

  if (error) {
    return (
      <div className="card">
        <h3>Run {runId}</h3>
        <p className="muted">{error}</p>
        <button onClick={onClose}>Close</button>
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="card">
        <h3>Run {runId}</h3>
        <p className="muted">Loading…</p>
        <button onClick={onClose}>Close</button>
      </div>
    );
  }
  const { run, context, usage } = detail;
  return (
    <div className="card" data-testid="run-inspector">
      <div className="row spread">
        <h3>
          Run {run.id.slice(0, 8)} — {run.provider}
        </h3>
        <button onClick={onClose}>Close</button>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <Badge value={run.run_status || run.provider_state} />
        <Badge value={run.stage || run.role} />
        <span className="muted mono">{run.role}</span>
      </div>
      <table style={{ marginTop: 8 }}>
        <tbody>
          <tr>
            <td>Observed model</td>
            <td className="mono">{usage?.observed_model ?? run.model_observed ?? "unknown"}</td>
          </tr>
          <tr>
            <td>Requested model</td>
            <td className="mono">{usage?.requested_model ?? run.model_requested ?? "unknown"}</td>
          </tr>
          <tr>
            <td>Stage / role</td>
            <td className="mono">
              {run.stage || "—"} / {run.role}
            </td>
          </tr>
          <tr>
            <td>Duration</td>
            <td className="mono">{run.duration_ms != null ? `${(run.duration_ms / 1000).toFixed(1)}s` : "—"}</td>
          </tr>
          <tr>
            <td>GG prompt</td>
            <td>
              <EstimateValue tokens={context?.estimated_prompt_tokens} />{" "}
              <span className="muted">({context?.prompt_chars ?? "—"} chars)</span>
            </td>
          </tr>
          <tr>
            <td>Provider input</td>
            <td>
              <UsageValue value={usage?.input_tokens_total} label="input" />
            </td>
          </tr>
          <tr>
            <td>Cache read / write</td>
            <td>
              <UsageValue value={usage?.cache_read_input_tokens} label="cache read" /> /{" "}
              <UsageValue value={usage?.cache_write_input_tokens} label="cache write" />
            </td>
          </tr>
          <tr>
            <td>Provider output</td>
            <td>
              <UsageValue value={usage?.output_tokens_total} label="output" />
            </td>
          </tr>
          <tr>
            <td>Source / completeness</td>
            <td className="mono">
              {usage?.source ?? "UNKNOWN"} / {usage?.completeness ?? "UNKNOWN"}
            </td>
          </tr>
          {!usage || usage.source === "UNKNOWN" ? (
            <tr>
              <td colSpan={2} className="muted">
                {run.provider === "agy"
                  ? "AGY usage unknown — provider usage telemetry not captured"
                  : "Usage unknown — not captured for this run"}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
      <div style={{ marginTop: 12 }}>
        <button onClick={() => setShowContext((v) => !v)}>{showContext ? "Hide Context" : "View Context"}</button>
      </div>
      {showContext ? (
        <div data-testid="run-context" style={{ marginTop: 8 }}>
          <h4>Context</h4>
          <table>
            <tbody>
              <tr>
                <td>Policy / template</td>
                <td className="mono">
                  {(run as unknown as Record<string, unknown>).context_policy_version as string} /{" "}
                  {(run as unknown as Record<string, unknown>).prompt_template_version as string}
                </td>
              </tr>
              <tr>
                <td>Budget / used / remaining</td>
                <td className="mono">
                  {context?.budget_estimated_tokens ?? "—"} / {context?.used_estimated_tokens ?? "—"} /{" "}
                  {context?.remaining_estimated_tokens ?? "—"} (est. tokens)
                </td>
              </tr>
              <tr>
                <td>Repeated-context ratio</td>
                <td className="mono">
                  {context?.repeated_context_ratio != null ? Number(context.repeated_context_ratio).toFixed(3) : "—"}
                </td>
              </tr>
              <tr>
                <td>Warnings</td>
                <td className="mono">{(context?.warnings ?? []).join(", ") || "—"}</td>
              </tr>
            </tbody>
          </table>
          {contextDetail ? (
            <div style={{ marginTop: 8 }}>
              {(Array.isArray((contextDetail as Record<string, unknown>).blocks)
                ? ((contextDetail as Record<string, unknown>).blocks as Array<Record<string, unknown>>)
                : []).map((b, i) => (
                <div key={i} className="row spread mono" style={{ fontSize: 12, padding: "2px 0" }}>
                  <span>
                    {String(b.block_type)} · {String(b.block_id)}
                  </span>
                  <span>
                    {String(b.priority)} · {String(b.representation)} · {b.included ? "INCLUDED" : "OMITTED"} ·{" "}
                    {String(b.reason)}
                  </span>
                </div>
              ))}
              {(!Array.isArray((contextDetail as Record<string, unknown>).blocks) ||
                ((contextDetail as Record<string, unknown>).blocks as unknown[]).length === 0) && (
                <p className="muted">No block detail (legacy manifest or not captured).</p>
              )}
            </div>
          ) : (
            <p className="muted">Loading context…</p>
          )}
          <p className="muted" style={{ fontSize: 12 }}>
            Raw prompts are never displayed. Manifests carry block metadata only.
          </p>
        </div>
      ) : null}
    </div>
  );
}
