import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { RepairCyclePanel } from "../components/RepairCyclePanel";

const mockRepairCycles = vi.fn();

vi.mock("../lib/api", () => ({
  api: {
    lifecycle: {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      repairCycles: (...args: any[]) => (mockRepairCycles as any)(...args),
      cancelRepairCycle: async () => ({ ok: true }),
    },
  },
}));

describe("RepairCyclePanel", () => {
  beforeEach(() => {
    mockRepairCycles.mockReset();
  });

  it("renders nothing when no repair cycles exist", async () => {
    mockRepairCycles.mockResolvedValue({ cycles: [], stats: {} });
    const { container } = render(<RepairCyclePanel projectId="prod-1" refresh={async () => {}} />);
    await waitFor(() => expect(mockRepairCycles).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="repair-panel"]')).toBeNull();
  });

  it("shows attempt lineage, reviewer, recheck, and stop reason", async () => {
    mockRepairCycles.mockResolvedValue({
      cycles: [
        {
          id: "rep-abc123",
          project_id: "prod-1",
          trigger_type: "CRITERION_FAILED",
          trigger_evidence_id: "crit-1",
          trigger_sha: "a".repeat(40),
          target_requirement_id: "R1",
          target_criterion_id: "R1-A1",
          target_finding_id: null,
          classification: "IMPLEMENTATION_DEFECT",
          status: "SUCCEEDED",
          max_attempts: 2,
          attempts_used: 1,
          stop_reason: null,
          gate_hint: null,
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
          attempts: [
            {
              id: "att-1",
              attempt_number: 1,
              provider: "opencode",
              base_sha: "a".repeat(40),
              result_sha: "b".repeat(40),
              outcome: "SUCCEEDED",
              review_reviewer: "claude",
              review_outcome: "passed",
              recheck_attempt_id: "crit-2",
              recheck_outcome: "passed",
              failure_signature: null,
            },
          ],
        },
      ],
      stats: {},
    });
    render(<RepairCyclePanel projectId="prod-1" refresh={async () => {}} />);
    await waitFor(() => expect(screen.getByTestId("repair-panel")).toBeDefined());
    expect(screen.getByText("SUCCEEDED")).toBeDefined();
    expect(screen.getByText(/reviewed by claude \(passed\)/)).toBeDefined();
    expect(screen.getByText(/recheck passed/)).toBeDefined();
  });

  it("shows stop reason for exhausted cycles", async () => {
    mockRepairCycles.mockResolvedValue({
      cycles: [
        {
          id: "rep-xyz",
          project_id: "prod-1",
          trigger_type: "CRITERION_FAILED",
          trigger_evidence_id: "crit-9",
          trigger_sha: "c".repeat(40),
          target_requirement_id: "R1",
          target_criterion_id: "R1-A1",
          target_finding_id: null,
          classification: "IMPLEMENTATION_DEFECT",
          status: "EXHAUSTED",
          max_attempts: 2,
          attempts_used: 2,
          stop_reason: "attempt budget exhausted",
          gate_hint: null,
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
          attempts: [],
        },
      ],
      stats: {},
    });
    render(<RepairCyclePanel projectId="prod-1" refresh={async () => {}} />);
    await waitFor(() => expect(screen.getByText("EXHAUSTED")).toBeDefined());
    expect(screen.getByText("attempt budget exhausted")).toBeDefined();
  });
});
