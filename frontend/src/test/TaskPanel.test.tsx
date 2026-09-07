import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TaskPanel } from "../components/TaskPanel";
import type { TaskRecord } from "../lib/types";

const baseTask: TaskRecord = {
  id: "t1", mission_id: "m1", role: "implementation", status: "RUNNING", task_type: "phase",
  title: "Create greeting", description: "Make src/greeting.py", preferred_providers: "[\"agy\"]",
  assigned_provider: "agy", workspace_scope: "[\"src/*\"]", resource_locks: "[]",
  max_attempts: 3, priority: 0, ready_at: null, started_at: null, provider_run_id: null,
  checkpoint_before: null, checkpoint_after: null, result: "{}", blocking_issue: null,
  dag_revision: 1, prompt: "", summary: "Working…", attempts: 0, created_at: "", finished_at: null,
};

describe("TaskPanel", () => {
  it("shows task title and status", () => {
    render(<TaskPanel task={baseTask} missionId="m1" onRefresh={() => {}} />);
    expect(screen.getByText("Create greeting")).toBeInTheDocument();
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it("shows assigned provider", () => {
    render(<TaskPanel task={baseTask} missionId="m1" onRefresh={() => {}} />);
    expect(screen.getByText(/Provider:/)).toBeInTheDocument();
  });

  it("shows cancel button for running task", () => {
    render(<TaskPanel task={baseTask} missionId="m1" onRefresh={() => {}} />);
    expect(screen.getByText("Cancel")).toBeInTheDocument();
  });

  it("shows retry button for failed task", () => {
    const failed = { ...baseTask, status: "FAILED" };
    render(<TaskPanel task={failed} missionId="m1" onRefresh={() => {}} />);
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });
});
