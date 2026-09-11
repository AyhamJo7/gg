import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LifecycleDetailPage } from "../pages/LifecycleDetailPage";

const mocks = vi.hoisted(() => ({ get: vi.fn(), cycles: vi.fn(), acceptance: vi.fn(), pause: vi.fn(), cancel: vi.fn() }));
vi.mock("../lib/api", () => ({ api: {
  lifecycle: { get: mocks.get, repairCycles: mocks.cycles, acceptance: mocks.acceptance, pause: mocks.pause, cancel: mocks.cancel },
  missions: { list: async () => [] },
} }));
vi.mock("../components/ProductOverview", () => ({ ProductOverview: ({ headline }: { headline: string }) => <h2>{headline}</h2> }));
function show() {
  render(<MemoryRouter initialEntries={["/lifecycle/product"]}><Routes><Route path="/lifecycle/:id" element={<LifecycleDetailPage />} /></Routes></MemoryRouter>);
}
describe("product operator actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.get.mockResolvedValue({ id: "product", name: "Validation service", state: "BLOCKED", gates: [], phases: [], paused: 0 });
    mocks.cycles.mockResolvedValue({ cycles: [] });
  });
  it("identifies autonomous repair and prevents competing acceptance or plan actions", async () => {
    mocks.cycles.mockResolvedValue({ cycles: [{ status: "REPAIRING" }] });
    show();
    expect(await screen.findByRole("heading", { name: "Repairing automatically" })).toBeInTheDocument();
    expect(screen.getByTestId("lifecycle-accept")).toBeDisabled();
    expect(screen.queryByTestId("lifecycle-plan")).not.toBeInTheDocument();
  });
  it("shows cancellation ahead of retained historical gates", async () => {
    mocks.get.mockResolvedValue({ id: "product", name: "Validation service", state: "CANCELLED", gates: [{ status: "open" }], phases: [], paused: 1 });
    show();
    expect(await screen.findByRole("heading", { name: "Cancelled" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });
  it("does not hide a human decision behind automatic repair", async () => {
    mocks.get.mockResolvedValue({ id: "product", name: "Validation service", state: "BLOCKED", gates: [{ status: "open" }], phases: [] });
    mocks.cycles.mockResolvedValue({ cycles: [{ status: "REPAIRING" }] });
    show();
    expect(await screen.findByRole("heading", { name: "Needs your decision" })).toBeInTheDocument();
  });
  it("explicitly requests an acceptance recheck", async () => {
    show();
    await waitFor(() => expect(screen.getByTestId("lifecycle-accept")).toBeEnabled());
    fireEvent.click(screen.getByTestId("lifecycle-accept"));
    await waitFor(() => expect(mocks.acceptance).toHaveBeenCalledWith("product", true));
  });
  it("does not cancel when confirmation is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(mocks.cancel).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
