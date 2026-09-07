import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { MissionControlPage } from "../pages/MissionControlPage";

const mockMission = {
  id: "m1", project_id: "p1", title: "Parallel Test", task: "test", status: "IMPLEMENTING",
  current_phase: "implementation", current_provider: null, autonomy: "BALANCED", profile: "balanced",
  providers_used: [], providers_failed: [], repair_cycles: 0, blocking_issue: null, git_head: null,
  scheduling_mode: "PARALLEL_SAFE", created_at: new Date().toISOString(), updated_at: new Date().toISOString(), finished_at: null,
};

const mockDetail = {
  ...mockMission,
  tasks: [
    { id: "a", mission_id: "m1", role: "implementation", status: "COMPLETED", title: "Greeting", description: "", preferred_providers: "[]", assigned_provider: "agy", workspace_scope: "[]", resource_locks: "[]", max_attempts: 3, priority: 0, ready_at: null, started_at: null, provider_run_id: null, checkpoint_before: null, checkpoint_after: null, result: "{}", blocking_issue: null, dag_revision: 1, prompt: "", summary: "Done", attempts: 0, created_at: "", finished_at: null },
    { id: "b", mission_id: "m1", role: "implementation", status: "RUNNING", title: "Farewell", description: "", preferred_providers: "[]", assigned_provider: "opencode", workspace_scope: "[]", resource_locks: "[]", max_attempts: 3, priority: 0, ready_at: null, started_at: null, provider_run_id: null, checkpoint_before: null, checkpoint_after: null, result: "{}", blocking_issue: null, dag_revision: 1, prompt: "", summary: "Working", attempts: 0, created_at: "", finished_at: null },
  ],
  gates: [], findings: [], runs: [], reviews: [], latest_review: null, degraded_review: false, latest_handoff: null, integrations: [],
};

const mockDag = {
  mission_id: "m1", scheduling_mode: "PARALLEL_SAFE",
  tasks: mockDetail.tasks,
  dependencies: [],
  branches: [],
  reservations: [],
  locks: [],
};

vi.mock("../lib/api", () => ({
  api: {
    missions: {
      list: () => Promise.resolve([mockMission]),
      get: () => Promise.resolve(mockDetail),
      dag: () => Promise.resolve(mockDag),
      action: () => Promise.resolve({ status: "ok" }),
      retry: () => Promise.resolve({ id: "m2" }),
      retryTask: () => Promise.resolve({ status: "ok" }),
      cancelTask: () => Promise.resolve({ status: "ok" }),
    },
    providers: { list: () => Promise.resolve([]) },
    projects: { git: () => Promise.resolve({ is_repo: true, branch: "main", head: "abc", modified: [], added: [], deleted: [], untracked: [], diff_stat: "", diff: "", recent_commits: [] }) },
  },
}));

vi.mock("../lib/ws", () => ({
  useMissionEvents: () => ({ terminal: [], events: [], connected: true }),
}));

describe("MissionControlPage parallel view", () => {
  it("shows PARALLEL badge for parallel missions", async () => {
    render(<MemoryRouter><MissionControlPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("PARALLEL")).toBeInTheDocument(), { timeout: 3000 });
  });

  it("shows task DAG", async () => {
    render(<MemoryRouter><MissionControlPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Task DAG" })).toBeInTheDocument(), { timeout: 3000 });
    expect(screen.getByTestId("dag-node-a")).toBeInTheDocument();
    expect(screen.getByTestId("dag-node-b")).toBeInTheDocument();
  });

  it("shows task panels for all tasks", async () => {
    render(<MemoryRouter><MissionControlPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Tasks (2)" })).toBeInTheDocument(), { timeout: 3000 });
    expect(screen.getByTestId("task-panel-a")).toBeInTheDocument();
    expect(screen.getByTestId("task-panel-b")).toBeInTheDocument();
  });
});
