import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AgentRelayView } from "../components/AgentRelay";
import { flagText, formatChars, formatDuration, formatKnownDuration, laneColorIndex, relayAttentionCount, relayLanes, runOutcomeLabel } from "../lib/relay";
import type { MissionRelay, RelayHandoff, RelayRun } from "../lib/types";

const mocks = vi.hoisted(() => ({ handoff: vi.fn() }));
vi.mock("../lib/api", () => ({ api: { missions: { handoff: mocks.handoff, relay: vi.fn() } } }));

const run = (over: Partial<RelayRun> = {}): RelayRun => ({
  kind: "run", id: "r1", at: "2026-09-12T01:00:00", provider: "claude", role: "planning", stage: "mission_plan",
  task_id: null, attempt_number: 1, retry_of_run_id: null, outcome: "SUCCEEDED", outcome_source: "run_status",
  failure_class: "NONE", exit_code: 0, started_at: null, finished_at: null, duration_ms: 642998, model_observed: null,
  commit_before: "aaaaaaaaa", commit_after: "aaaaaaaaa", changed_commit: false, summary: "planned", summary_truncated: false,
  context: { capture_status: "CAPTURED", schema_version: "v2", prompt_chars: 12944, estimated_prompt_tokens: 3236,
    block_count: 3, warnings: [], thin_evidence_blocks: [], reduced_blocks: [] },
  flags: [], ...over,
});
const handoff = (over: Partial<RelayHandoff> = {}): RelayHandoff => ({
  kind: "handoff", id: "h1", at: "2026-09-12T01:10:00", from_provider: "codex", to_provider: "agy", role: "review",
  git_head: null, stored_chars: 16443, preview: "handoff preview", preview_truncated: true, ...over,
});
const thinReview = run({
  id: "r-agy", provider: "agy", role: "review", duration_ms: 462767, flags: ["THIN_REQUIRED_EVIDENCE"],
  context: { capture_status: "CAPTURED", schema_version: "v2", prompt_chars: 25700, estimated_prompt_tokens: null,
    block_count: 4, warnings: [], reduced_blocks: [],
    thin_evidence_blocks: [{ block_type: "GIT_DIFF", block_id: "git_diff", included_chars: 131, original_chars: 131 }] },
});
const lost = run({ id: "r-codex", provider: "codex", role: "testing", outcome: "FAILED", duration_ms: 438392, flags: ["LOST_WORK"] });
const relay: MissionRelay = {
  mission_id: "m1",
  timeline: [run(), lost, handoff(), thinReview, {
    kind: "review", at: "2026-09-12T01:20:00", id: "rv", reviewer: "agy", independent: false,
    degradation_reason: "writer provenance incomplete (cannot certify independence)", writer_set: ["claude", "opencode"],
    reviewer_in_writer_set: false, parsed: true, reviewed_base_sha: "2a20ca71", reviewed_head_sha: "bf39469b", created_at: null,
  }],
  findings: [
    { id: "f1", severity: "MEDIUM", category: "security", file: "ingest.py", status: "open", description: "capture upload not rate limited",
      recommended_fix: "", text_truncated: false, origin_review_id: "rv", origin_sha: "bf39469b3d", resolved_review_id: null,
      resolved_sha: null, verified_by: null, inherited_from_mission_id: null, created_at: null, resolved_at: null },
    { id: "f2", severity: "HIGH", category: "vat", file: "vat.py", status: "resolved", description: "no VAT breakdown",
      recommended_fix: "", text_truncated: false, origin_review_id: "rv0", origin_sha: "1111111111", resolved_review_id: "rv1",
      resolved_sha: "2222222222", verified_by: "claude", inherited_from_mission_id: "44cdae6dbf4d45c5", created_at: null, resolved_at: null },
  ],
  providers: [{ provider: "claude", runs: 2, succeeded: 1, not_succeeded: 0, in_flight: 0, outcome_unknown: 0, known_duration_ms: 642998, unknown_duration_runs: 1, roles: ["planning"] }],
  runs_truncated: false,
  limits: { handoff_preview_chars: 1200, thin_evidence_block_chars: 500, lost_work_min_ms: 60000, run_limit: 500 },
};

describe("relay helpers", () => {
  it("keeps unknown duration and size unknown, never zero", () => {
    expect(formatDuration(null)).toBe("unknown");
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(438392)).toBe("7m 18s");
    expect(formatDuration(3_900_000)).toBe("1h 05m");
    expect(formatChars(null)).toBe("unknown size");
    expect(formatKnownDuration(0, 4, 4)).toBe("duration not recorded");
    expect(formatKnownDuration(60000, 3, 1)).toBe("1m 00s + 1 run(s) of unknown length");
  });
  it("orders lanes by first appearance and adds a GG lane only for unrecorded senders", () => {
    expect(relayLanes(relay)).toEqual(["claude", "codex", "agy"]);
    expect(relayLanes({ timeline: [handoff({ from_provider: null, to_provider: "claude" })] })).toEqual(["GG", "claude"]);
  });
  it("explains thin evidence with the recorded sizes", () => {
    expect(flagText("THIN_REQUIRED_EVIDENCE", thinReview, 500)).toBe("Small required evidence (under 500 chars): GIT_DIFF 131 chars");
    expect(flagText("LOST_WORK", lost, 500)).toBe("Ran 7m 18s and did not succeed (failed)");
  });
  it("assigns stable lane colors and counts flagged runs", () => {
    expect([0, 1, 2].map(i => laneColorIndex(["claude", "codex", "agy"], ["claude", "codex", "agy"][i], 6))).toEqual([0, 1, 2]);
    expect(runOutcomeLabel("SUCCEEDED")).toBe("Succeeded");
    expect(relayAttentionCount(relay)).toBe(2);
  });
});

describe("AgentRelayView", () => {
  it("shows the dogfood story: lost work, thin evidence, uncertified review", () => {
    render(<AgentRelayView relay={relay} />);
    expect(screen.getByText("lost work")).toBeInTheDocument();
    expect(screen.getByText("thin evidence")).toBeInTheDocument();
    expect(screen.getByText("not certified")).toBeInTheDocument();
    expect(screen.getByText(/writer provenance incomplete/)).toBeInTheDocument();
    expect(screen.getByText(/handed off to/)).toHaveTextContent("codex handed off to agy for review");
    expect(screen.getByText(/1 run\(s\) of unknown length/)).toBeInTheDocument();
  });
  it("shows finding lineage without calling repair claims verified", () => {
    render(<AgentRelayView relay={relay} />);
    const items = screen.getAllByRole("listitem").filter(li => li.classList.contains("lineage-item"));
    expect(items[0]).toHaveTextContent("capture upload not rate limited");
    expect(items[1]).toHaveTextContent("verified fixed at 22222222 by claude");
    expect(items[1]).toHaveTextContent("inherited from mission 44cdae6d");
  });
  it("loads the full handoff on demand", async () => {
    mocks.handoff.mockResolvedValue({ content: "FULL HANDOFF TEXT", stored_chars: 17, truncated: false });
    render(<AgentRelayView relay={relay} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 16,443 chars" }));
    await waitFor(() => expect(screen.getByText("FULL HANDOFF TEXT")).toBeInTheDocument());
    expect(mocks.handoff).toHaveBeenCalledWith("m1", "h1");
  });
  it("reports a failed handoff load instead of showing nothing", async () => {
    mocks.handoff.mockRejectedValue(new Error("500"));
    render(<AgentRelayView relay={relay} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all 16,443 chars" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the full handoff");
  });
  it("offers the run inspector from a run step", () => {
    const onInspect = vi.fn();
    render(<AgentRelayView relay={relay} onInspectRun={onInspect} />);
    const step = screen.getByText("thin evidence").closest("details")!;
    fireEvent.click(within(step).getByRole("button", { name: "Open run inspector" }));
    expect(onInspect).toHaveBeenCalledWith("r-agy");
  });
  it("has an explicit empty state", () => {
    render(<AgentRelayView relay={{ ...relay, timeline: [], findings: [] }} />);
    expect(screen.getByText("No provider has run for this mission yet.")).toBeInTheDocument();
  });
  it("states recorded reduction facts instead of inventing a budget cause", () => {
    const reduced = run({ flags: ["CONTEXT_REDUCED"], context: { ...run().context!, reduced_blocks: [
      { block_type: "PHASE_CONTEXT", block_id: "p", included_chars: 900, original_chars: 5000, representation: "COMPACT", reason: null },
    ] } });
    expect(flagText("CONTEXT_REDUCED", reduced, 500)).toBe("Included in reduced form: PHASE_CONTEXT 900 chars of 5,000 chars (COMPACT)");
  });
  it("spans a reviewer-less review across lanes instead of crediting the first provider", () => {
    const { container } = render(<AgentRelayView relay={{ ...relay, timeline: [run(), { ...relay.timeline[4], reviewer: "" } as MissionRelay["timeline"][number]] }} />);
    const review = container.querySelector(".relay-review") as HTMLElement;
    expect(review.style.gridColumn).toBe("1 / -1");
  });
  it("labels an unrecorded outcome as such", () => {
    render(<AgentRelayView relay={{ ...relay, timeline: [run({ outcome: "UNKNOWN", outcome_source: "legacy" })] }} />);
    expect(screen.getByText(/Outcome not recorded/)).toBeInTheDocument();
  });
});
