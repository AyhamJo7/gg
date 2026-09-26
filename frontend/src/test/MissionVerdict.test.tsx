import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MissionVerdictBadge, ReviewTrustCard } from "../components/MissionVerdict";
import { missionVerdict, reviewIndependenceText } from "../lib/operator";
import type { MissionTrust, ReviewSummary } from "../lib/types";

const review = (over: Partial<ReviewSummary> = {}): ReviewSummary => ({
  id: "r1", reviewer: "agy", independent: false,
  degradation_reason: "writer provenance incomplete (cannot certify independence)",
  writer_set: ["claude", "opencode"], reviewer_in_writer_set: false, parsed: true,
  reviewed_base_sha: "aaaaaaaa11", reviewed_head_sha: "bbbbbbbb22", created_at: null, ...over,
});
const trust = (over: Partial<MissionTrust> = {}): MissionTrust => ({
  open_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 0, LOW: 0 },
  repair_claimed_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 0, LOW: 0 }, unrecognized_severity: 0, inherited_available: true,
  review: review({ independent: true, degradation_reason: null }), review_count: 1, ...over,
});

describe("mission verdict", () => {
  it("does not render the RechnungsRadar completion as clean", () => {
    const v = missionVerdict({ status: "COMPLETED", trust: trust({
      open_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 1, LOW: 0 }, review: review(),
    }) });
    expect(v.label).toBe("Completed with caveats");
    expect(v.caveats).toEqual(["1 open MEDIUM", "review not certified independent"]);
    expect(v.needsAttention).toBe(true);
  });
  it("calls a completion clean only when no caveat is recorded", () => {
    expect(missionVerdict({ status: "COMPLETED", trust: trust() })).toMatchObject({ tone: "clean", needsAttention: false });
  });
  it("treats a missing review on completion as a caveat, not as clean", () => {
    expect(missionVerdict({ status: "COMPLETED", trust: trust({ review: null }) }).caveats).toContain("no review recorded");
  });
  it("keeps LOW-only caveats visible without demanding attention", () => {
    const v = missionVerdict({ status: "COMPLETED", trust: trust({ open_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 0, LOW: 2 } }) });
    expect(v).toMatchObject({ label: "Completed with caveats", needsAttention: false });
  });
  it("renders unknown trust as unknown, never clean", () => {
    const v = missionVerdict({ status: "COMPLETED" });
    expect(v.tone).toBe("unknown");
    expect(v.caveats).toEqual(["review and finding details unavailable"]);
  });
  it("flags unverified repair claims and unparsed reviews", () => {
    const v = missionVerdict({ status: "COMPLETED", trust: trust({
      repair_claimed_findings: { BLOCKER: 0, HIGH: 2, MEDIUM: 0, LOW: 0 }, review: review({ independent: true, parsed: false }) }) });
    expect(v.caveats).toEqual(["2 HIGH repairs claimed, not verified", "review output not parsed"]);
  });
  it("stopped missions always need attention; paused ones do not", () => {
    expect(missionVerdict({ status: "UNVERIFIED", trust: trust() }).needsAttention).toBe(true);
    expect(missionVerdict({ status: "CANCELLED", trust: trust() }).needsAttention).toBe(false);
    expect(missionVerdict({ status: "PAUSED", trust: trust() })).toMatchObject({ tone: "active", needsAttention: false });
  });
  it("never lists one finding twice", () => {
    const v = missionVerdict({ status: "COMPLETED", trust: trust({
      open_findings: { BLOCKER: 0, HIGH: 1, MEDIUM: 0, LOW: 0 },
      repair_claimed_findings: { BLOCKER: 0, HIGH: 1, MEDIUM: 0, LOW: 0 } }) });
    expect(v.caveats).toEqual(["1 open HIGH", "1 HIGH repair claimed, not verified"]);
  });
  it("reports unreadable retry history", () => {
    expect(missionVerdict({ status: "COMPLETED", trust: trust({ inherited_available: false }) }).caveats).toContain("retry history findings unavailable");
  });
});

describe("review independence text", () => {
  it("never claims self-review without writer-set evidence", () => {
    const t = reviewIndependenceText(review());
    expect(t.title).toBe("Review not certified as independent");
    expect(t.detail).toMatch(/writer provenance incomplete/);
  });
  it("states self-review when the reviewer is a recorded writer", () => {
    expect(reviewIndependenceText(review({ reviewer_in_writer_set: true })).badge).toBe("SELF-REVIEW");
  });
  it("says when no reason was recorded", () => {
    expect(reviewIndependenceText(review({ degradation_reason: null })).detail).toBe("No reason was recorded.");
  });
});

describe("ReviewTrustCard", () => {
  it("renders recorded facts and no fabricated cause (dogfood §19)", () => {
    render(<ReviewTrustCard review={review()} />);
    expect(screen.queryByText(/also performed the implementation/)).not.toBeInTheDocument();
    expect(screen.getByText("claude, opencode")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaa → bbbbbbbb")).toBeInTheDocument();
  });
  it("renders nothing for an independent review", () => {
    const { container } = render(<ReviewTrustCard review={review({ independent: true })} />);
    expect(container).toBeEmptyDOMElement();
  });
  it("badge lists caveats next to the verdict", () => {
    render(<MissionVerdictBadge mission={{ status: "COMPLETED", trust: trust({ review: null }) }} />);
    expect(screen.getByTestId("mission-verdict")).toHaveTextContent("Completed with caveats");
    expect(screen.getByTestId("mission-verdict")).toHaveTextContent("no review recorded");
  });
});

describe("missionAttentionList", () => {
  const base = { trust: null, updated_at: "2026-09-12T01:00:00Z", retry_of_mission_id: null } as const;
  it("drops superseded missions and puts stops before caveats", async () => {
    const { missionAttentionList } = await import("../lib/operator");
    const missions = [
      { ...base, id: "caveat", status: "COMPLETED", updated_at: "2026-09-12T05:00:00Z",
        trust: { open_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 1, LOW: 0 }, repair_claimed_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 0, LOW: 0 },
          unrecognized_severity: 0, inherited_available: true, review: null, review_count: 0 } },
      { ...base, id: "old-fail", status: "FAILED" },
      { ...base, id: "retry", status: "WAITING_FOR_HUMAN", retry_of_mission_id: "old-fail" },
    ] as unknown as import("../lib/types").Mission[];
    expect(missionAttentionList(missions).map(m => m.id)).toEqual(["retry", "caveat"]);
  });
});
