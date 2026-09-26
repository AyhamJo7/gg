import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DagGraph } from "../components/DagGraph";
import type { TaskRecord, TaskDependency } from "../lib/types";

const tasks: TaskRecord[] = [
  { id: "a", mission_id: "m1", role: "implementation", status: "COMPLETED", task_type: "phase", title: "Greeting", description: "", preferred_providers: "[]", assigned_provider: "agy", workspace_scope: "[]", resource_locks: "[]", max_attempts: 3, priority: 0, ready_at: null, started_at: null, provider_run_id: null, checkpoint_before: null, checkpoint_after: null, input_sha: null, result_sha: null, result: "{}", blocking_issue: null, dag_revision: 1, prompt: "", summary: "", attempts: 0, created_at: "", finished_at: null },
  { id: "b", mission_id: "m1", role: "implementation", status: "RUNNING", task_type: "phase", title: "Farewell", description: "", preferred_providers: "[]", assigned_provider: "opencode", workspace_scope: "[]", resource_locks: "[]", max_attempts: 3, priority: 0, ready_at: null, started_at: null, provider_run_id: null, checkpoint_before: null, checkpoint_after: null, input_sha: null, result_sha: null, result: "{}", blocking_issue: null, dag_revision: 1, prompt: "", summary: "", attempts: 0, created_at: "", finished_at: null },
];

const deps: TaskDependency[] = [];

describe("DagGraph", () => {
  it("renders tasks", () => {
    render(<DagGraph tasks={tasks} dependencies={deps} />);
    expect(screen.getByText("Greeting")).toBeInTheDocument();
    expect(screen.getByText("Farewell")).toBeInTheDocument();
  });

  it("calls onTaskClick when a task is clicked", () => {
    const handler = vi.fn();
    render(<DagGraph tasks={tasks} dependencies={deps} onTaskClick={handler} />);
    fireEvent.click(screen.getByTestId("dag-node-a"));
    expect(handler).toHaveBeenCalledWith("a");
  });

  it("shows empty state when no tasks", () => {
    render(<DagGraph tasks={[]} dependencies={[]} />);
    expect(screen.getByText("No tasks in this mission yet.")).toBeInTheDocument();
  });
  it("exposes keyboard-native task controls and named dependencies", () => {
    render(<DagGraph tasks={tasks} dependencies={[{ from_task_id: "a", to_task_id: "b", created_at: "" }]} onTaskClick={() => {}} />);
    expect(screen.getByRole("button", { name: "Farewell: RUNNING" })).toHaveAttribute("type", "button");
    expect(screen.getByText("Depends on: Greeting")).toBeInTheDocument();
  });
});

describe("DagGraph edges", () => {
  it("draws one edge per dependency and keeps a text list for assistive tech", () => {
    render(<DagGraph tasks={tasks} dependencies={[{ from_task_id: "a", to_task_id: "b", created_at: "" }]} activeTaskId="b" />);
    const svg = screen.getByTestId("dag-edges");
    const paths = svg.querySelectorAll("path.dag-edge");
    expect(paths).toHaveLength(1);
    expect(paths[0].getAttribute("class")).toContain("focused");
    expect(paths[0].getAttribute("marker-end")).toBe("url(#dag-arrow-focused)");
    expect(screen.getByText("Dependency list (1)")).toBeInTheDocument();
  });
});

describe("DagGraph edge tone", () => {
  it("never styles an edge into a stale task as done", () => {
    const stale = tasks.map(t => t.id === "b" ? { ...t, status: "STALE" } : t);
    render(<DagGraph tasks={stale} dependencies={[{ from_task_id: "a", to_task_id: "b", created_at: "" }]} />);
    expect(screen.getByTestId("dag-edges").querySelector("path.dag-edge")!.getAttribute("class")).toContain("pending");
  });
});
