import type { ProviderHealth } from "../lib/types";
import { statusColor } from "../lib/status";

export function ProviderStrip({ providers, active }: { providers: ProviderHealth[]; active?: string | null }) {
  return (
    <div className="provider-strip">
      {providers.map((p) => {
        const isActive = p.name === active;
        const display = isActive ? "ACTIVE" : p.cooldown_until && p.state !== "AVAILABLE" ? "COOLDOWN" : p.state;
        return (
          <div
            key={p.name}
            className="provider-chip"
            style={isActive ? { borderColor: "var(--accent)" } : undefined}
            title={p.last_error ?? p.version ?? ""}
          >
            <span className="name">{p.name}</span>
            <span className={`state`} style={{ color: `var(--${statusColor(display) === "" ? "text-faint" : cssColor(statusColor(display))})` }}>
              {display}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function cssColor(color: string): string {
  return { green: "green", yellow: "yellow", red: "red", blue: "blue", purple: "purple", orange: "orange" }[color] ?? "text-faint";
}
