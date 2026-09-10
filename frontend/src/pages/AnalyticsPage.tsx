import { usePolling } from "../lib/hooks";
import { api } from "../lib/api";
import { Badge } from "../components/Badge";

export function AnalyticsPage() {
  const { data } = usePolling(() => api.analytics(), 10000);
  const { data: usage } = usePolling(() => api.usageAnalytics(), 10000);
  const { data: context } = usePolling(() => api.contextAnalytics(), 10000);

  return (
    <div>
      <h1>Analytics</h1>
      <p className="muted">Local-only statistics inferred from actual executions. Nothing leaves this machine.</p>
      <div className="grid-2" style={{ marginTop: 12 }}>
        <div className="card">
          <h3>Missions by status</h3>
          {data && Object.keys(data.missions_by_status).length > 0 ? (
            Object.entries(data.missions_by_status).map(([status, n]) => (
              <div key={status} className="row spread" style={{ padding: "3px 0" }}>
                <Badge value={status} />
                <span className="mono">{n}</span>
              </div>
            ))
          ) : (
            <p className="muted">No missions yet</p>
          )}
        </div>
        <div className="card">
          <h3>Review findings by severity</h3>
          {data && Object.keys(data.findings_by_severity).length > 0 ? (
            Object.entries(data.findings_by_severity).map(([sev, n]) => (
              <div key={sev} className="row spread" style={{ padding: "3px 0" }}>
                <Badge value={sev} />
                <span className="mono">{n}</span>
              </div>
            ))
          ) : (
            <p className="muted">No findings recorded</p>
          )}
        </div>
      </div>
      <div className="card" style={{ marginTop: 12 }}>
        <h3>Provider usage (inferred from executions — estimates)</h3>
        <table>
          <thead>
            <tr><th>Provider</th><th>Runs</th><th>Successes</th><th>Rate limits</th><th>Avg duration</th></tr>
          </thead>
          <tbody>
            {(data?.provider_stats ?? []).map((p) => (
              <tr key={p.provider}>
                <td style={{ textTransform: "capitalize", fontWeight: 600 }}>{p.provider}</td>
                <td className="mono">{p.runs}</td>
                <td className="mono">{p.successes}</td>
                <td className="mono">{p.rate_limits}</td>
                <td className="mono">{p.avg_seconds ? Math.round(p.avg_seconds) + "s" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card" style={{ marginTop: 12 }}>
        <h3>Token telemetry coverage (honest aggregates)</h3>
        <p className="muted" style={{ fontSize: 12 }}>
          {usage?.note ?? "Totals include only runs with captured usage; unknown runs are excluded, never zero-filled."}
        </p>
        <table>
          <thead>
            <tr><th>Provider</th><th>Runs</th><th>With usage</th><th>Complete</th><th>Partial</th><th>Unknown</th><th>Input</th><th>Output</th></tr>
          </thead>
          <tbody>
            {(usage?.by_provider ?? []).map((p) => (
              <tr key={p.provider}>
                <td style={{ textTransform: "capitalize", fontWeight: 600 }}>{p.provider}</td>
                <td className="mono">{p.runs}</td>
                <td className="mono">{p.runs_with_usage}</td>
                <td className="mono">{p.complete_runs}</td>
                <td className="mono">{p.partial_runs}</td>
                <td className="mono">{p.unknown_runs}</td>
                <td className="mono">{p.input_tokens ?? "—"}</td>
                <td className="mono">{p.output_tokens ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(!usage || usage.by_provider.length === 0) && <p className="muted">No token telemetry captured yet</p>}
      </div>
      <div className="card" style={{ marginTop: 12 }}>
        <h3>Context efficiency (GG prompt estimates, char4-v1)</h3>
        <p className="muted" style={{ fontSize: 12 }}>
          {context?.note ?? "GG prompt estimates are distinct from provider-observed usage."}
        </p>
        <table>
          <thead>
            <tr><th>Policy</th><th>Runs</th><th>Avg est. tokens</th><th>Avg repeated ratio</th></tr>
          </thead>
          <tbody>
            {(context?.by_policy ?? []).map((p) => (
              <tr key={p.policy}>
                <td className="mono">{p.policy || "—"}</td>
                <td className="mono">{p.runs}</td>
                <td className="mono">{p.avg_estimated_tokens != null ? Math.round(p.avg_estimated_tokens) : "—"}</td>
                <td className="mono">{p.avg_repeated_ratio != null ? Number(p.avg_repeated_ratio).toFixed(3) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr><th>Role</th><th>Runs</th><th>Avg est. tokens</th><th>Avg repeated ratio</th></tr>
          </thead>
          <tbody>
            {(context?.by_role ?? []).map((p) => (
              <tr key={p.role}>
                <td className="mono">{p.role}</td>
                <td className="mono">{p.runs}</td>
                <td className="mono">{p.avg_estimated_tokens != null ? Math.round(p.avg_estimated_tokens) : "—"}</td>
                <td className="mono">{p.avg_repeated_ratio != null ? Number(p.avg_repeated_ratio).toFixed(3) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {context && (
          <p className="muted mono" style={{ fontSize: 12 }}>
            Legacy avg: {context.legacy_avg_estimated_tokens != null ? Math.round(context.legacy_avg_estimated_tokens) : "—"} ·{" "}
            Compiled avg: {context.compiled_avg_estimated_tokens != null ? Math.round(context.compiled_avg_estimated_tokens) : "—"}
          </p>
        )}
        {context && Object.keys(context.warning_counts).length > 0 && (
          <div style={{ marginTop: 8 }}>
            {Object.entries(context.warning_counts).map(([w, n]) => (
              <div key={w} className="row spread" style={{ padding: "2px 0" }}>
                <span className="mono" style={{ fontSize: 12 }}>{w}</span>
                <span className="mono">{n}</span>
              </div>
            ))}
          </div>
        )}
        {(!context || (context.by_policy.length === 0 && context.by_role.length === 0)) && (
          <p className="muted">No context manifests yet</p>
        )}
      </div>
    </div>
  );
}
