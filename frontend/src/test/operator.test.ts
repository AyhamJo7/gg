import { describe, expect, it } from "vitest";
import { missionHref, productAttention, operatorLabel } from "../lib/operator";
import type { ProductProjectSummary, RepairCycle } from "../lib/types";

const project = { id: "p", state: "BLOCKED", open_gates: 0 } as ProductProjectSummary;
const repairing = [{ id: "c", status: "REPAIRING" }] as RepairCycle[];
describe("operator attention", () => {
  it("does not resurrect a cancelled product because historical gates remain", () => {
    expect(productAttention({ ...project, state: "CANCELLED", open_gates: 1 }, repairing)).toMatchObject({ needsAttention: false, label: "Cancelled" });
  });
  it("does not describe paused repair as actively progressing", () => {
    expect(productAttention({ ...project, paused: 1 }, repairing)).toMatchObject({ needsAttention: false, label: "Paused" });
  });
  it("does not ask for intervention during automatic repair", () => {
    expect(productAttention(project, repairing)).toMatchObject({ needsAttention: false, label: "Repairing automatically" });
  });
  it("never hides an open Human Gate behind automatic repair", () => {
    expect(productAttention({ ...project, open_gates: 1 }, repairing).needsAttention).toBe(true);
  });
  it("shows exhausted repair as requiring attention", () => {
    expect(productAttention(project, [{ status: "EXHAUSTED" } as RepairCycle]).needsAttention).toBe(true);
  });
  it("distinguishes a provider wait from repair execution", () => {
    expect(productAttention(project, [{ status: "WAITING_FOR_PROVIDER" } as RepairCycle]).label).toBe("Repair waiting for provider");
  });
  it("preserves task identity in links and labels unknown states without inventing success", () => {
    expect(missionHref("mission/one")).toBe("/missions?mission=mission%2Fone");
    expect(operatorLabel("UNVERIFIED")).toBe("Not verified");
    expect(operatorLabel("UNRECOGNIZED_STATE")).toBe("unrecognized state");
  });
});
