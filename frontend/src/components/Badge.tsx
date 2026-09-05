export function statusColor(status: string): string {
  switch (status) {
    case "COMPLETED":
    case "AVAILABLE":
    case "passed":
      return "green";
    case "PLANNING":
    case "IMPLEMENTING":
    case "TESTING":
    case "REVIEWING":
    case "REPAIRING":
    case "FINAL_VALIDATION":
    case "ANALYZING":
    case "BUSY":
      return "blue";
    case "PAUSED":
    case "WAITING_FOR_PROVIDER":
    case "COOLDOWN":
      return "yellow";
    case "RATE_LIMITED":
    case "TIMED_OUT":
      return "orange";
    case "FAILED":
    case "CRASHED":
    case "AUTH_REQUIRED":
    case "BLOCKER":
      return "red";
    case "WAITING_FOR_HUMAN":
    case "UNVERIFIED":
    case "HIGH":
      return "purple";
    default:
      return "";
  }
}

export function Badge({ value, pulse }: { value: string; pulse?: boolean }) {
  return (
    <span className={`badge ${statusColor(value)}`}>
      <span className="dot" style={pulse ? { animation: "pulse 1.2s infinite" } : undefined} />
      {value}
    </span>
  );
}
