import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TaskLogPanel } from "../components/TaskLogPanel";

const run = {
  id: "r1",
  provider: "agy",
  role: "implementation",
  failure_class: "NONE",
  provider_state: "COMPLETED",
  exit_code: 0,
  started_at: new Date().toISOString(),
  finished_at: new Date().toISOString(),
  summary: "done",
};

const taskLogs = vi.fn(
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  async (_missionId: string, _taskId: string, _tailBytes?: number) => ({
    stdout: "hello from stdout",
    stderr: "",
    run,
    stdout_size: 18,
    stderr_size: 0,
    stdout_truncated: false,
    stderr_truncated: false,
  }),
);

vi.mock("../lib/api", () => ({
  api: {
    missions: {
      taskLogs: (missionId: string, taskId: string, tailBytes?: number) =>
        taskLogs(missionId, taskId, tailBytes),
    },
  },
}));

describe("TaskLogPanel", () => {
  it("shows provider and stdout", async () => {
    taskLogs.mockClear();
    render(<TaskLogPanel missionId="m1" taskId="t1" active={false} />);
    await waitFor(() => expect(screen.getByText(/hello from stdout/)).toBeInTheDocument());
    expect(screen.getByText(/agy/)).toBeInTheDocument();
    expect(screen.getByTestId("task-log-live")).toHaveTextContent("final");
  });

  it("polls while the task is active and stops when it finishes", async () => {
    taskLogs.mockClear();
    const { rerender } = render(
      <TaskLogPanel missionId="m1" taskId="t1" active={true} pollMs={50} />,
    );
    await waitFor(() => expect(taskLogs.mock.calls.length).toBeGreaterThanOrEqual(1));
    await waitFor(() => expect(taskLogs.mock.calls.length).toBeGreaterThanOrEqual(3), {
      timeout: 2000,
    });
    expect(screen.getByTestId("task-log-live")).toHaveTextContent("live");
    rerender(<TaskLogPanel missionId="m1" taskId="t1" active={false} pollMs={50} />);
    const calls = taskLogs.mock.calls.length;
    await new Promise((r) => setTimeout(r, 300));
    expect(taskLogs.mock.calls.length).toBe(calls);
  });

  it("reloads when switching tasks", async () => {
    taskLogs.mockClear();
    const { rerender } = render(<TaskLogPanel missionId="m1" taskId="t1" active={false} />);
    await waitFor(() => expect(screen.getByText(/hello from stdout/)).toBeInTheDocument());
    rerender(<TaskLogPanel missionId="m1" taskId="t2" active={false} />);
    await waitFor(() => expect(taskLogs).toHaveBeenLastCalledWith("m1", "t2", 65536));
  });

  it("notes truncated tails", async () => {
    taskLogs.mockClear();
    taskLogs.mockResolvedValueOnce({
      stdout: "tail…",
      stderr: "",
      run,
      stdout_size: 300000,
      stderr_size: 0,
      stdout_truncated: true,
      stderr_truncated: false,
    });
    render(<TaskLogPanel missionId="m1" taskId="t1" active={false} />);
    await waitFor(() => expect(screen.getByText(/older output truncated/)).toBeInTheDocument());
  });
});
