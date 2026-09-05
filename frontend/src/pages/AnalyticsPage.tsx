import { usePolling } from "../lib/hooks";
import { api } from "../lib/api";
import { Badge } from "../components/Badge";

export function AnalyticsPage() {
  const { data } = usePolling(() => api.analytics(), 10000);

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
    </div>
  );
}
