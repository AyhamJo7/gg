import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { MissionControlPage } from "../pages/MissionControlPage";

const failed = {
  id: "m1", project_id: "p1", title: "Stopped run", task: "t", status: "UNVERIFIED", current_phase: null, current_provider: null,
  autonomy: "BALANCED", profile: "balanced", providers_used: [], providers_failed: [], repair_cycles: 0,
  blocking_issue: "Docker/network unavailable in the verification sandbox", git_head: null, scheduling_mode: "SEQUENTIAL",
  created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T01:00:00Z", finished_at: "2026-09-10T01:00:00Z", trust: null,
};
const mocks = vi.hoisted(() => ({ retry: vi.fn() }));
vi.mock("../lib/api", () => ({ api: {
  missions: {
    list: () => Promise.resolve([failed]),
    get: () => Promise.resolve({ ...failed, tasks: [], gates: [], findings: [], runs: [], reviews: [], latest_review: null,
      degraded_review: false, latest_handoff: null, integrations: [], inherited_findings: [] }),
    dag: () => Promise.resolve(null),
    relay: () => Promise.resolve({ mission_id: "m1", timeline: [], findings: [], providers: [], runs_truncated: false,
      limits: { handoff_preview_chars: 1, thin_evidence_block_chars: 1, lost_work_min_ms: 1, run_limit: 1 } }),
    retry: mocks.retry,
  },
  providers: { list: () => Promise.resolve([]) },
  projects: { git: () => Promise.resolve(null) },
} }));
vi.mock("../lib/ws", () => ({ useMissionEvents: () => ({ terminal: [], events: [], connected: true }) }));

describe("blocking issue retry", () => {
  it("offers retry next to the blocker and opens the new mission", async () => {
    mocks.retry.mockResolvedValue({ id: "m2" });
    render(<MemoryRouter><MissionControlPage /></MemoryRouter>);
    const card = (await screen.findByRole("heading", { name: "Blocking issue" })).closest(".card") as HTMLElement;
    expect(card).toHaveTextContent("Docker/network unavailable");
    fireEvent.click(card.querySelector("button")!);
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("m1"));
  });
  it("surfaces a failed retry instead of silently doing nothing", async () => {
    mocks.retry.mockRejectedValue(new Error("409 conflict"));
    render(<MemoryRouter><MissionControlPage /></MemoryRouter>);
    const card = (await screen.findByRole("heading", { name: "Blocking issue" })).closest(".card") as HTMLElement;
    fireEvent.click(card.querySelector("button")!);
    expect(await screen.findByRole("alert")).toHaveTextContent("409 conflict");
  });
});
