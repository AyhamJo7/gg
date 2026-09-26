import type { MissionRelay, RelayEntry, RelayFlag, RelayRun } from "./types";

const MS_PER_SECOND = 1000;
const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;
/** Sender column label when a handoff did not record a provider. */
export const UNRECORDED_SENDER = "GG";

/** Human duration; unknown stays unknown, never "0s". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "unknown";
  const totalSeconds = Math.round(ms / MS_PER_SECOND);
  const minutes = Math.floor(totalSeconds / SECONDS_PER_MINUTE);
  const hours = Math.floor(minutes / MINUTES_PER_HOUR);
  if (hours > 0) return `${hours}h ${String(minutes % MINUTES_PER_HOUR).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(totalSeconds % SECONDS_PER_MINUTE).padStart(2, "0")}s`;
  return `${totalSeconds}s`;
}

export function formatChars(n: number | null | undefined): string {
  if (n === null || n === undefined) return "unknown size";
  return `${n.toLocaleString("en-US")} chars`;
}

/** Lanes in order of first appearance, so the relay reads left to right in
 * the order agents joined the mission. The unrecorded sender lane exists
 * only when a handoff actually lacks a sender. */
export function relayLanes(relay: Pick<MissionRelay, "timeline">): string[] {
  const lanes: string[] = [];
  const add = (name: string | null | undefined) => {
    if (name && !lanes.includes(name)) lanes.push(name);
  };
  for (const e of relay.timeline) {
    if (e.kind === "run") add(e.provider);
    else if (e.kind === "handoff") { add(e.from_provider ?? UNRECORDED_SENDER); add(e.to_provider); }
    else add(e.reviewer);
  }
  return lanes;
}

/** Lane for an entry; null means "span all lanes" (no recorded actor). */
export function entryLane(entry: RelayEntry): string | null {
  if (entry.kind === "run") return entry.provider;
  if (entry.kind === "review") return entry.reviewer || null;
  return entry.to_provider;
}

export function flagText(flag: RelayFlag, run: RelayRun, thinLimit: number): string {
  switch (flag) {
    case "THIN_REQUIRED_EVIDENCE": {
      const blocks = run.context?.thin_evidence_blocks ?? [];
      const detail = blocks.map(b => `${b.block_type} ${formatChars(b.included_chars)}`).join(", ");
      return `Small required evidence (under ${thinLimit} chars): ${detail}`;
    }
    case "CONTEXT_REDUCED": {
      const blocks = run.context?.reduced_blocks ?? [];
      const detail = blocks.map(b => {
        const recorded = [b.representation, b.reason].filter(Boolean).join(", ");
        return `${b.block_type} ${formatChars(b.included_chars)} of ${formatChars(b.original_chars)}${recorded ? ` (${recorded})` : ""}`;
      }).join("; ");
      return `Included in reduced form: ${detail}`;
    }
    case "LOST_WORK":
      return `Ran ${formatDuration(run.duration_ms)} and did not succeed (${run.outcome.toLowerCase()})`;
    case "CONTEXT_NOT_CAPTURED":
      return "What this provider was told was not captured for this run";
  }
}

export const FLAG_SHORT: Record<RelayFlag, string> = {
  THIN_REQUIRED_EVIDENCE: "thin evidence",
  CONTEXT_REDUCED: "reduced context",
  LOST_WORK: "lost work",
  CONTEXT_NOT_CAPTURED: "context unknown",
};

/** Lane color by lane position: distinct for up to paletteSize agents in
 * one mission (a name hash collided for codex/opencode/agy). */
export function laneColorIndex(lanes: string[], name: string, paletteSize: number): number {
  const index = lanes.indexOf(name);
  return index < 0 ? 0 : index % paletteSize;
}

const RUN_OUTCOME_LABELS: Record<string, string> = {
  SUCCEEDED: "Succeeded", FAILED: "Failed", TIMED_OUT: "Timed out", CANCELLED: "Cancelled", CRASHED: "Crashed",
  RUNNING: "Running", STARTING: "Starting", PREPARED: "Prepared", WAITING_FOR_CAPACITY: "Waiting for capacity",
  CANCELLING: "Cancelling", UNKNOWN: "Not known",
};

/** Invocation outcome wording (distinct from repair-cycle vocabulary). */
export function runOutcomeLabel(outcome: string): string {
  return RUN_OUTCOME_LABELS[outcome] ?? outcome.replaceAll("_", " ").toLowerCase();
}

export function relayAttentionCount(relay: MissionRelay): number {
  return relay.timeline.filter(e => e.kind === "run" && e.flags.some(f => f !== "CONTEXT_NOT_CAPTURED")).length;
}

/** Total provider time; never shows "0s" when no duration was recorded. */
export function formatKnownDuration(knownMs: number, runs: number, unknownRuns: number): string {
  if (runs > 0 && unknownRuns >= runs) return "duration not recorded";
  const known = formatDuration(knownMs);
  return unknownRuns ? `${known} + ${unknownRuns} run(s) of unknown length` : known;
}
