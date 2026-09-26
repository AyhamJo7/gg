import { operatorLabel } from "./operator";
import type { OrchestratorEvent } from "./types";

export type ActivityTone = "attention" | "good" | "bad" | "info";

export interface ActivityItem {
  id: string;
  at: string;
  missionId: string | null;
  missionTitle: string | null;
  productId: string | null;
  text: string;
  tone: ActivityTone;
  /** Something the operator should look at, counted as unread. */
  attention: boolean;
}

export interface ActivityEvent extends OrchestratorEvent {
  mission_title?: string | null;
}

/** Event types that are routine noise in a global feed. */
const QUIET_TYPES = new Set([
  "PROVIDER_OUTPUT", "PROVIDER_RESERVED", "PROVIDER_RELEASED", "LOCK_ACQUIRED", "LOCK_RELEASED",
  "WORKTREE_CREATED", "WORKTREE_REMOVED", "TASK_READY", "TASK_CLAIMED", "DAG_VALIDATED", "PROVIDER_SELECTED",
]);

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function withReason(text: string, reason: unknown): string {
  const r = str(reason);
  return r ? `${text}: ${r}` : text;
}

/** One-line, evidence-only description; unknown types fall back to their name. */
export function describeEvent(event: ActivityEvent): ActivityItem | null {
  if (QUIET_TYPES.has(event.type)) return null;
  const p = event.payload ?? {};
  const provider = str(p.provider);
  const base = {
    id: event.id,
    at: event.created_at,
    missionId: event.mission_id,
    missionTitle: event.mission_title ?? null,
    productId: str(p.product_project_id),
  };
  const make = (text: string, tone: ActivityTone, attention = false): ActivityItem => ({ ...base, text, tone, attention });
  switch (event.type) {
    case "MISSION_CREATED": return make("Mission created", "info");
    case "MISSION_STATUS_CHANGED": {
      const status = str(p.status);
      // FAILED and WAITING_FOR_HUMAN are followed by MISSION_FAILED /
      // HUMAN_GATE_CREATED, which carry the attention; count each stop once.
      const attention = status === "UNVERIFIED";
      const stopped = !!status && ["WAITING_FOR_HUMAN", "FAILED", "UNVERIFIED"].includes(status);
      return make(withReason(`Now ${status ? operatorLabel(status).toLowerCase() : "in an unrecorded state"}`, p.reason ?? p.blocking_issue), stopped ? "attention" : "info", attention);
    }
    case "MISSION_COMPLETED": return make("Mission completed — check its verdict for caveats", "good", true);
    case "MISSION_FAILED": return make(withReason("Mission stopped", p.reason), "bad", true);
    case "MISSION_PAUSED": return make("Mission paused", "info");
    case "MISSION_RESUMED": return make("Mission resumed", "info");
    case "PHASE_STARTED": return make(`Started ${str(p.phase) ?? "a phase"}`, "info");
    case "PHASE_COMPLETED": return make(`Finished ${str(p.phase) ?? "a phase"}`, "info");
    case "PROVIDER_STARTED": return make(`${provider ?? "A provider"} started${str(p.role) ? ` ${p.role}` : ""}`, "info");
    case "PROVIDER_RATE_LIMITED": return make(`${provider ?? "A provider"} was rate limited`, "bad");
    case "PROVIDER_FAILED": return make(withReason(`${provider ?? "A provider"} failed`, p.failure), "bad");
    case "PROVIDER_HEALTH_CHANGED": return make(`${provider ?? "A provider"} is now ${operatorLabel(str(p.state) ?? "UNKNOWN").toLowerCase()}`, "info");
    case "HANDOFF_CREATED": return make(`Handoff prepared${str(p.role) ? ` for ${p.role}` : ""}`, "info");
    case "TEST_PASSED": return make("Verification passed", "good");
    case "TEST_FAILED": return make("Verification failed", "bad");
    case "REVIEW_FINDING_CREATED": {
      const severity = str(p.severity);
      const serious = severity === "BLOCKER" || severity === "HIGH";
      return make(`${severity ?? "A"} finding: ${str(p.description) ?? "no description recorded"}`, serious ? "attention" : "info", serious);
    }
    case "REVIEW_RECORDED":
      return p.independent === true
        ? make(`Independent review by ${str(p.review_provider) ?? "an unrecorded reviewer"}`, "good")
        : make(withReason(`Review by ${str(p.review_provider) ?? "an unrecorded reviewer"} not certified independent`, p.degradation_reason), "attention", true);
    case "HUMAN_GATE_CREATED": return make(withReason("Decision needed", p.reason), "attention", true);
    case "HUMAN_GATE_RESOLVED": return make("Decision recorded", "info");
    case "GIT_CHECKPOINT_FAILED": return make("Git checkpoint failed", "bad", true);
    case "MERGE_CONFLICT": return make("Merge conflict during integration", "attention", true);
    case "TASK_FAILED": return make(withReason("Task failed", p.reason), "bad");
    case "TASK_BLOCKED": return make(withReason("Task blocked", p.reason), "attention");
    case "INTEGRATION_COMPLETED": return make("Integration completed", "good");
    case "TASK_STARTED": return make(`${provider ?? "A provider"} started a task${str(p.role) ? ` (${p.role})` : ""}`, "info");
    case "TASK_COMPLETED": return make(`Task completed${provider ? ` by ${provider}` : ""}`, "info");
    case "GIT_CHECKPOINT_CREATED": return make(`Checkpoint committed${str(p.sha) ? ` at ${String(p.sha).slice(0, 8)}` : ""}`, "info");
    case "PRODUCT_PROJECT_CREATED": return make(`Product created${str(p.name) ? `: ${p.name}` : ""}`, "info");
    case "PRODUCT_STATUS_CHANGED": return make(`Product now ${str(p.status) ? operatorLabel(String(p.status)).toLowerCase() : "in an unrecorded state"}`, "info");
    case "PRODUCT_PLAN_READY": return make("Product plan ready for review", "attention", true);
    case "PRODUCT_GATE_CREATED": return make(withReason("Product decision needed", p.title ?? p.reason), "attention", true);
    case "PRODUCT_DELIVERED": return make("Product delivered", "good", true);
    case "PRODUCT_ACCEPTANCE_RECORDED": return make("Acceptance recorded", "info");
    default: return make(operatorLabel(event.type), "info");
  }
}

export function activityHref(item: ActivityItem): string | null {
  if (item.productId) return `/lifecycle/${encodeURIComponent(item.productId)}`;
  if (item.missionId) return `/missions?mission=${encodeURIComponent(item.missionId)}`;
  return null;
}

const JUST_NOW_SECONDS = 45;
const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;
const RELATIVE_HOURS_LIMIT = 48;

export function relativeTime(iso: string, now: number): string {
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "at an unknown time";
  if (seconds < JUST_NOW_SECONDS) return "just now";
  const minutes = Math.round(seconds / SECONDS_PER_MINUTE);
  if (minutes < MINUTES_PER_HOUR) return `${minutes}m ago`;
  const hours = Math.round(minutes / MINUTES_PER_HOUR);
  if (hours < RELATIVE_HOURS_LIMIT) return `${hours}h ago`;
  return new Date(iso).toLocaleDateString();
}
