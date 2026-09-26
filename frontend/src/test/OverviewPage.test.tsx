import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "../pages/OverviewPage";

const mocks = vi.hoisted(() => ({ list: vi.fn(), cycles: vi.fn(), missions: vi.fn() }));
vi.mock("../lib/api", () => ({ api: {
  lifecycle: { list: mocks.list, repairCycles: mocks.cycles },
  missions: { list: mocks.missions }, providers: { list: async () => [] },
} }));
const cleanTrust = {
  unresolved_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 0, LOW: 0 }, unresolved_other: 0, unverified_repairs: 0,
  inherited_unresolved: 0, inherited_available: true, review_count: 1,
  review: { id: "r", reviewer: "codex", independent: true, degradation_reason: null, writer_set: ["claude"],
    reviewer_in_writer_set: false, parsed: true, reviewed_base_sha: null, reviewed_head_sha: null, created_at: null },
};
const mission = {
  id: "m", project_id: "p", title: "M", task: "t", status: "COMPLETED", current_phase: null, current_provider: null,
  autonomy: "BALANCED", profile: "balanced", providers_used: [], providers_failed: [], repair_cycles: 0,
  blocking_issue: null, git_head: null, scheduling_mode: "SEQUENTIAL", created_at: "2026-09-12T00:00:00Z",
  updated_at: "2026-09-12T01:00:00Z", finished_at: "2026-09-12T01:00:00Z",
};
describe("overview", () => {
  beforeEach(() => { mocks.cycles.mockResolvedValue({ cycles: [] }); mocks.missions.mockResolvedValue([]); });
  it("provides distinct first-time entry points", async () => {
    mocks.list.mockResolvedValue([]);
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    await screen.findByText("Nothing is running. Start with an idea or a task in an existing repository.");
    expect(screen.getAllByRole("link", { name: "Build a product" })[0]).toHaveAttribute("href", "/lifecycle?create=1");
  });
  it("links directly to a product needing a decision", async () => {
    mocks.list.mockResolvedValue([{ id: "needs-me", name: "Billing app", state: "WAITING_FOR_HUMAN", open_gates: 1, blocking_reason: "Configure credentials", phase_counts: {} }]);
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: /Billing app/ })).toHaveAttribute("href", "/lifecycle/needs-me");
    expect(screen.getByText("Needs your decision →")).toBeInTheDocument();
  });
  it("does not present a failed load as an empty healthy workspace", async () => {
    mocks.list.mockRejectedValue(new Error("unavailable"));
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Updates unavailable"));
    expect(screen.queryByText(/No product decisions/)).not.toBeInTheDocument();
  });
  it("surfaces a completed standalone mission with caveats as needing attention", async () => {
    mocks.list.mockResolvedValue([]);
    mocks.missions.mockResolvedValue([{ ...mission, id: "rr", title: "RechnungsRadar", trust: { ...cleanTrust,
      unresolved_findings: { BLOCKER: 0, HIGH: 0, MEDIUM: 1, LOW: 0 }, review: { ...cleanTrust.review, independent: false } } }]);
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    const section = (await screen.findByRole("heading", { name: /Needs your attention/ })).closest("section")!;
    expect(section).toHaveTextContent("RechnungsRadar");
    expect(section).toHaveTextContent("1 unresolved MEDIUM");
    expect(section.querySelector("a")).toHaveAttribute("href", "/missions?mission=rr");
  });
  it("lists clean completed missions under recent outcomes", async () => {
    mocks.list.mockResolvedValue([]);
    mocks.missions.mockResolvedValue([{ ...mission, id: "ok", title: "Clean run", trust: cleanTrust }]);
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    const section = (await screen.findByRole("heading", { name: "Recent outcomes" })).closest("section")!;
    await waitFor(() => expect(section).toHaveTextContent("Clean run"));
    expect(section).toHaveTextContent("Completed · no caveats recorded");
    expect(screen.getByText(/Nothing is waiting on you/)).toBeInTheDocument();
  });
  it("shows running missions as in progress", async () => {
    mocks.list.mockResolvedValue([]);
    mocks.missions.mockResolvedValue([{ ...mission, id: "run", title: "Live work", status: "IMPLEMENTING", current_provider: "claude", finished_at: null }]);
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    const section = (await screen.findByRole("heading", { name: "In progress & ready to start" })).closest("section")!;
    await waitFor(() => expect(section).toHaveTextContent("Mission · claude"));
  });
});
