import { useEffect, useRef, useState } from "react";
import type { TerminalLine } from "../lib/ws";

export function Terminal({ lines }: { lines: TerminalLine[] }) {
  const [autoscroll, setAutoscroll] = useState(true);
  const [filter, setFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState<string>("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoscroll && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [lines, autoscroll]);

  const providers = [...new Set(lines.map((l) => l.provider))];
  const visible = lines.filter(
    (l) =>
      (!providerFilter || l.provider === providerFilter) &&
      (!filter || l.text.toLowerCase().includes(filter.toLowerCase())),
  );

  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <input
          placeholder="Search output…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ maxWidth: 220 }}
        />
        <select value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)} style={{ maxWidth: 140 }}>
          <option value="">all providers</option>
          {providers.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <button onClick={() => setAutoscroll((v) => !v)}>{autoscroll ? "⏸ pause scroll" : "▶ auto-scroll"}</button>
        <button onClick={() => navigator.clipboard.writeText(visible.map((l) => l.text).join("\n"))}>copy</button>
      </div>
      <div className="terminal" ref={ref} data-testid="terminal">
        {visible.length === 0 ? (
          <div className="empty">No provider output yet</div>
        ) : (
          visible.map((l) => (
            <div className="tline" key={l.id}>
              <span className={`tprov ${l.provider}`}>[{l.provider}]</span>
              <span>{l.text}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
