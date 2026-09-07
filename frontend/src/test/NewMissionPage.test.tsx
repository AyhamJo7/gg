import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { NewMissionPage } from "../pages/NewMissionPage";

vi.mock("../lib/api", () => ({
  api: {
    projects: { list: () => Promise.resolve([{ id: "p1", name: "Test", path: "/tmp/test" }]) },
    settings: { profiles: () => Promise.resolve({}) },
    missions: {
      create: () => Promise.resolve({ id: "m1" }),
      action: () => Promise.resolve({ status: "started" }),
      updateDag: () => Promise.resolve({ status: "dag updated" }),
    },
  },
}));

describe("NewMissionPage scheduling mode", () => {
  it("defaults to Sequential", async () => {
    render(<MemoryRouter><NewMissionPage /></MemoryRouter>);
    const select = await screen.findByTestId("scheduling-mode");
    expect(select).toHaveValue("SEQUENTIAL");
  });

  it("switches to Parallel Safe and shows DAG mode options", async () => {
    render(<MemoryRouter><NewMissionPage /></MemoryRouter>);
    const select = await screen.findByTestId("scheduling-mode");
    fireEvent.change(select, { target: { value: "PARALLEL_SAFE" } });
    expect(await screen.findByText("Auto-plan (AI decomposes the mission)")).toBeInTheDocument();
    expect(screen.getByText("Manual DAG")).toBeInTheDocument();
  });

  it("shows manual DAG editor when selected", async () => {
    render(<MemoryRouter><NewMissionPage /></MemoryRouter>);
    const modeSelect = await screen.findByTestId("scheduling-mode");
    fireEvent.change(modeSelect, { target: { value: "PARALLEL_SAFE" } });
    const manualRadio = await screen.findByLabelText("Manual DAG");
    fireEvent.click(manualRadio);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /task dag editor/i })).toBeInTheDocument();
    });
    expect(screen.getByText("+ Add Task")).toBeInTheDocument();
  });
});
