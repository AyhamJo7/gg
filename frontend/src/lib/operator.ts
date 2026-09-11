import type { ProductProjectSummary, RepairCycle } from "./types";

export const ACTIVE_REPAIR_STATES = new Set([
  "CREATED", "CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING", "WAITING_FOR_PROVIDER",
]);
export const FINISHED_PRODUCT_STATES = new Set(["DELIVERED", "CANCELLED", "FAILED"]);

const LABELS: Record<string, string> = {
  DRAFT: "Ready to plan", PLANNING: "Creating plan", PLAN_READY: "Plan ready for review",
  EXECUTING: "Building", REVIEWING: "Reviewing", FINAL_ACCEPTANCE: "Checking acceptance",
  WAITING_FOR_HUMAN: "Needs your decision", WAITING_FOR_PROVIDER: "Waiting for a provider",
  BLOCKED: "Needs attention", DELIVERED: "Delivered", CANCELLED: "Cancelled", FAILED: "Stopped",
  PAUSED: "Paused", CLAIMED: "Preparing to run", RUNNING: "Running", COMPLETED: "Completed",
  UNVERIFIED: "Not verified", STALE: "Needs a fresh attempt", PENDING: "Not started",
  READY: "Ready", REPAIRING: "Repairing", RECHECKING: "Rechecking repaired code",
  EXHAUSTED: "Repair budget exhausted", SUCCEEDED: "Repair passed", VALID: "Passed",
  MISSING: "Not yet recorded", UNKNOWN: "Not known", AVAILABLE: "Available", BUSY: "Busy",
  DISABLED: "Disabled", COOLDOWN: "Cooling down", QUOTA_EXHAUSTED: "Quota exhausted",
  RATE_LIMITED: "Rate limited", UNAVAILABLE: "Unavailable",
};

export function operatorLabel(state: string): string {
  return LABELS[state.toUpperCase()] ?? state.replaceAll("_", " ").toLowerCase();
}

export function productAttention(project: ProductProjectSummary, cycles: RepairCycle[] = []) {
  if (FINISHED_PRODUCT_STATES.has(project.state)) return {
    needsAttention: project.state === "FAILED",
    activeRepair: undefined,
    label: operatorLabel(project.state),
  };
  if (project.paused) return {
    needsAttention: project.open_gates > 0,
    activeRepair: undefined,
    label: project.open_gates > 0 ? "Paused · decision waiting" : "Paused",
  };
  const activeRepair = cycles.find(c => ACTIVE_REPAIR_STATES.has(c.status));
  // A human gate is never hidden by simultaneous autonomous work.
  const needsAttention = project.open_gates > 0 ||
    (!activeRepair && ["BLOCKED", "WAITING_FOR_HUMAN", "FAILED", "PLAN_READY"].includes(project.state));
  return {
    needsAttention,
    activeRepair,
    label: project.open_gates > 0 ? "Needs your decision" : activeRepair
      ? activeRepair.status === "WAITING_FOR_PROVIDER" ? "Repair waiting for provider" : "Repairing automatically"
      : operatorLabel(project.state),
  };
}

export function missionHref(id: string): string {
  return `/missions?mission=${encodeURIComponent(id)}`;
}
