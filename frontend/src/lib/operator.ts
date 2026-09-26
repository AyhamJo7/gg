import type { Mission, MissionTrust, ProductProjectSummary, RepairCycle, ReviewSummary } from "./types";

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

export const TERMINAL_MISSION_STATES = new Set(["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED"]);
const STOPPED_MISSION_STATES = new Set(["FAILED", "UNVERIFIED", "WAITING_FOR_HUMAN", "BLOCKED"]);
const SEVERITIES = ["BLOCKER", "HIGH", "MEDIUM", "LOW"] as const;
/** Severities that make a finished mission need an operator look. */
const ATTENTION_SEVERITIES = new Set<string>(["BLOCKER", "HIGH", "MEDIUM"]);

export type VerdictTone = "clean" | "caveat" | "stopped" | "active" | "unknown";

export interface MissionVerdict {
  label: string;
  tone: VerdictTone;
  caveats: string[];
  needsAttention: boolean;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Operator description of why a review was not certified. Never infers a
 * cause: self-review is claimed only when the recorded writer set contains
 * the reviewer; otherwise the recorded reason is shown as-is. */
export function reviewIndependenceText(review: ReviewSummary): { title: string; badge: string; detail: string } {
  const reason = review.degradation_reason?.trim() || "No reason was recorded.";
  if (review.reviewer_in_writer_set) {
    return {
      title: "Self-review — the reviewer also wrote part of this candidate",
      badge: "SELF-REVIEW",
      detail: reason,
    };
  }
  return { title: "Review not certified as independent", badge: "UNCERTIFIED", detail: reason };
}

export function trustCaveats(trust: MissionTrust, status: string): { caveats: string[]; serious: boolean } {
  const caveats: string[] = [];
  let serious = false;
  for (const sev of SEVERITIES) {
    const n = trust.unresolved_findings[sev] ?? 0;
    if (n > 0) {
      caveats.push(`${n} unresolved ${sev}`);
      if (ATTENTION_SEVERITIES.has(sev)) serious = true;
    }
  }
  if (trust.unresolved_other > 0) {
    caveats.push(`${trust.unresolved_other} unresolved (unrecognized severity)`);
    serious = true;
  }
  if (trust.unverified_repairs > 0) {
    caveats.push(`${plural(trust.unverified_repairs, "repair claim")} not verified fixed`);
    serious = true;
  }
  if (!trust.inherited_available) {
    caveats.push("retry history findings unavailable");
    serious = true;
  }
  if (trust.review) {
    if (!trust.review.independent) {
      caveats.push(trust.review.reviewer_in_writer_set ? "self-review" : "review not certified independent");
      serious = true;
    }
    if (trust.review.parsed === false) {
      caveats.push("review output not parsed");
      serious = true;
    }
  } else if (status === "COMPLETED") {
    caveats.push("no review recorded");
    serious = true;
  }
  return { caveats, serious };
}

/** A finished state is not a clean state: fold recorded caveats into the
 * verdict so COMPLETED-with-open-findings never reads like a spotless run. */
export function missionVerdict(mission: Pick<Mission, "status" | "trust">): MissionVerdict {
  const status = mission.status;
  const base = operatorLabel(status);
  if (!mission.trust) {
    return {
      label: base,
      tone: STOPPED_MISSION_STATES.has(status) ? "stopped" : TERMINAL_MISSION_STATES.has(status) ? "unknown" : "active",
      caveats: status === "COMPLETED" ? ["review and finding details unavailable"] : [],
      needsAttention: STOPPED_MISSION_STATES.has(status),
    };
  }
  const { caveats, serious } = trustCaveats(mission.trust, status);
  if (STOPPED_MISSION_STATES.has(status)) return { label: base, tone: "stopped", caveats, needsAttention: true };
  if (status === "CANCELLED") return { label: base, tone: "stopped", caveats, needsAttention: false };
  if (status === "COMPLETED") {
    return caveats.length
      ? { label: "Completed with caveats", tone: "caveat", caveats, needsAttention: serious }
      : { label: "Completed · no caveats recorded", tone: "clean", caveats, needsAttention: false };
  }
  return { label: base, tone: "active", caveats, needsAttention: false };
}
