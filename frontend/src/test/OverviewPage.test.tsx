import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "../pages/OverviewPage";

const mocks = vi.hoisted(() => ({ list: vi.fn(), cycles: vi.fn() }));
vi.mock("../lib/api", () => ({ api: {
  lifecycle: { list: mocks.list, repairCycles: mocks.cycles },
  missions: { list: async () => [] }, providers: { list: async () => [] },
} }));
describe("overview", () => {
  beforeEach(() => { mocks.cycles.mockResolvedValue({ cycles: [] }); });
  it("provides distinct first-time entry points", async () => {
    mocks.list.mockResolvedValue([]);
    render(<MemoryRouter><OverviewPage /></MemoryRouter>);
    await screen.findByText("No active product builds. Start with an idea or a task in an existing repository.");
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
});
