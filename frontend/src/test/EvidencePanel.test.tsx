import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { EvidencePanel } from "../components/EvidencePanel";

const mockEvidence = vi.fn();

vi.mock("../lib/api", () => ({
  api: {
    lifecycle: {
      // Lazy reference: the factory runs during import collection, before
      // the mock const below is initialized.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      evidence: (...args: any[]) => (mockEvidence as any)(...args),
    },
  },
}));

describe("EvidencePanel", () => {
  beforeEach(() => {
    mockEvidence.mockReset();
  });

  it("shows stale review with reviewed vs current SHA, never raw prompts", async () => {
    mockEvidence.mockResolvedValue({
      candidate_sha: "456def7890".padEnd(40, "0"),
      plan_revision: 2,
      writers: [{ actor_type: "PROVIDER", provider: "opencode", result_sha: "456def7890".padEnd(40, "0") }],
      writers_complete: true,
      review: {
        state: "STALE",
        phases: [
          {
            phase_id: "phase-1",
            candidate_sha: "456def7890".padEnd(40, "0"),
            state: "STALE",
            reviewer: "agy",
            detail: "reviewed 123abc, current 456def",
          },
        ],
      },
      verification: { state: "VALID" },
      criteria: { total: 2, passed: 2, stale: 0, failed: 0, missing: 0, waived: 0 },
      fresh_checkout: { state: "VALID" },
      delivery_ready: false,
      blocking_reasons: ["phase foundation changed after review (reviewed 123abc, now 456def)"],
    });
    render(<EvidencePanel projectId="prod-1" />);
    await waitFor(() => expect(screen.getByText("BLOCKED")).toBeDefined());
    expect(screen.getByText(/reviewed 123abc, now 456def/)).toBeDefined();
    expect(screen.queryByText(/sk-ant-/)).toBeNull();
  });

  it("shows READY with writers and phase attempts", async () => {
    mockEvidence.mockResolvedValue({
      candidate_sha: "9d4f0000".padEnd(40, "1"),
      plan_revision: 1,
      writers: [
        { actor_type: "PROVIDER", provider: "opencode", result_sha: "9d4f0000".padEnd(40, "1") },
        { actor_type: "HUMAN_OPERATOR", provider: null, result_sha: "9d4f0000".padEnd(40, "2") },
      ],
      writers_complete: true,
      review: { state: "VALID", phases: [] },
      verification: { state: "VALID" },
      criteria: { total: 1, passed: 1, stale: 0, failed: 0, missing: 0, waived: 0 },
      fresh_checkout: { state: "VALID" },
      delivery_ready: true,
      blocking_reasons: [],
      phase_attempts: [
        { phase_id: "phase-1", attempt_number: 1, trigger: "INITIAL", status: "COMPLETED", result_sha: "9d4f0000".padEnd(40, "1") },
      ],
    });
    render(<EvidencePanel projectId="prod-1" />);
    await waitFor(() => expect(screen.getAllByText("READY").length).toBeGreaterThan(0));
    expect(screen.getByText(/HUMAN_OPERATOR/)).toBeDefined();
  });

  it("shows an error state without crashing", async () => {
    mockEvidence.mockRejectedValue(new Error("nope"));
    render(<EvidencePanel projectId="prod-1" />);
    await waitFor(() => expect(screen.getByText("evidence unavailable")).toBeDefined());
  });
  it("accepts the minimal draft response without inventing evidence", async () => {
    mockEvidence.mockResolvedValue({ candidate_sha: null, delivery_ready: false, blocking_reasons: ["no target repo"] });
    render(<EvidencePanel projectId="draft" />);
    expect(await screen.findByRole("heading", { name: "No candidate yet" })).toBeInTheDocument();
    expect(screen.queryByText("READY")).not.toBeInTheDocument();
  });
});
