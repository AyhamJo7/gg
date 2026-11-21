import { statusColor } from "../lib/status";

export function Badge({ value, pulse }: { value: string; pulse?: boolean }) {
  return (
    <span className={`badge ${statusColor(value)}`}>
      <span className="dot" style={pulse ? { animation: "pulse 1.2s infinite" } : undefined} />
      {value}
    </span>
  );
}
