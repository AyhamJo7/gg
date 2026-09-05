import { useState } from "react";
import { api } from "../lib/api";
import { usePolling } from "../lib/hooks";
import { Badge } from "../components/Badge";

export function ProvidersPage() {
  const { data: providers, refresh } = usePolling(() => api.providers.list(), 5000);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});

  const test = async (name: string) => {
    setTesting(name);
    try {
      const r = await api.providers.test(name);
      setTestResult((prev) => ({
        ...prev,
        [name]: r.installed ? `OK — ${r.version ?? "detected"}` : "not installed",
      }));
    } catch (e) {
      setTestResult((prev) => ({ ...prev, [name]: (e as Error).message }));
    } finally {
      setTesting(null);
      refresh();
    }
  };

  return (
    <div>
      <h1>Providers</h1>
      <p className="muted">Local AI CLI subscriptions. Health, cooldowns, and usage inferred from actual executions.</p>
      <div className="card" style={{ marginTop: 12, overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Provider</th><th>State</th><th>Installed</th><th>Version</th><th>Path</th>
              <th>Runs</th><th>Success</th><th>Rate limits</th><th>Cooldown</th><th>Last error</th><th></th>
            </tr>
          </thead>
          <tbody>
            {providers?.map((p) => {
              const rate = p.total_runs ? Math.round((p.successful_runs / p.total_runs) * 100) : 0;
              return (
                <tr key={p.name} data-testid={`provider-${p.name}`}>
                  <td style={{ fontWeight: 600, textTransform: "capitalize" }}>{p.name}</td>
                  <td><Badge value={p.state} /></td>
                  <td>{p.installed ? "✓" : "✗"}</td>
                  <td className="mono muted" style={{ maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis" }}>{p.version ?? "—"}</td>
                  <td className="mono muted" style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{p.executable_path ?? "—"}</td>
                  <td className="mono">{p.total_runs}</td>
                  <td className="mono">{rate}%</td>
                  <td className="mono">{p.rate_limit_events}</td>
                  <td className="mono muted">{p.cooldown_until ? new Date(p.cooldown_until).toLocaleTimeString() : "—"}</td>
                  <td className="muted" style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis" }} title={p.last_error ?? ""}>
                    {p.last_error ? p.last_error.slice(0, 60) : "—"}
                  </td>
                  <td>
                    <div className="row">
                      <button onClick={() => test(p.name)} disabled={testing === p.name}>
                        {testing === p.name ? "…" : "Test"}
                      </button>
                      <button onClick={() => api.providers.toggle(p.name, p.state === "DISABLED").then(refresh)}>
                        {p.state === "DISABLED" ? "Enable" : "Disable"}
                      </button>
                    </div>
                    {testResult[p.name] && <div className="faint" style={{ fontSize: 11 }}>{testResult[p.name]}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
