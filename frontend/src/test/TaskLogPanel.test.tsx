import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TaskLogPanel } from "../components/TaskLogPanel";

const run: {
  id: string;
  provider: string;
  role: string;
  failure_class: string;
  provider_state: string;
  exit_code: number;
  started_at: string;
  finished_at: string | null;
  summary: string;
} = {
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
  async (_missionId: string, _taskId: string, _tailBytes?: number, _signal?: AbortSignal) => ({
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
      taskLogs: (missionId: string, taskId: string, tailBytes?: number, signal?: AbortSignal) =>
        taskLogs(missionId, taskId, tailBytes, signal),
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
    await waitFor(() =>
      expect(taskLogs).toHaveBeenLastCalledWith("m1", "t2", 65536, expect.anything()),
    );
  });

  it("mounts a single request and refetches on terminal transition", async () => {
    taskLogs.mockClear();
    const { rerender } = render(
      <TaskLogPanel missionId="m1" taskId="t1" active={true} pollMs={50} />,
    );
    await waitFor(() => expect(taskLogs.mock.calls.length).toBeGreaterThanOrEqual(1));
    const before = taskLogs.mock.calls.length;
    rerender(<TaskLogPanel missionId="m1" taskId="t1" active={false} pollMs={50} />);
    await waitFor(() =>
      expect(taskLogs.mock.calls.length).toBeGreaterThanOrEqual(before + 1),
    );
    expect(screen.getByTestId("task-log-live")).toHaveTextContent("final");
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

describe("TaskLogPanel request lifecycle", () => {
  const payload = (text: string) => ({
    stdout: text,
    stderr: "",
    run,
    stdout_size: text.length,
    stderr_size: 0,
    stdout_truncated: false,
    stderr_truncated: false,
  });

  it("never overlaps requests with slow responses", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    let release!: () => void;
    taskLogs.mockReset();
    taskLogs.mockImplementation(
      async () =>
        new Promise((resolve) => {
          inFlight += 1;
          maxInFlight = Math.max(maxInFlight, inFlight);
          release = () =>
            resolve({
              ...payload("slow"),
              run: { ...run, finished_at: null },
            });
        }),
    );
    render(<TaskLogPanel missionId="m1" taskId="t1" active={true} pollMs={20} />);
    await waitFor(() => expect(inFlight).toBe(1));
    await new Promise((r) => setTimeout(r, 150));
    expect(maxInFlight).toBe(1);
    release();
    await waitFor(() => expect(screen.getByText("slow")).toBeInTheDocument());
  });

  it("aborts superseded task and ignores its late response", async () => {
    const aborted: boolean[] = [];
    let releaseT1!: (v: string) => void;
    taskLogs.mockReset();
    taskLogs.mockImplementation(
      async (_m: string, t: string, _b?: number, signal?: AbortSignal) =>
        new Promise((resolve) => {
          if (t === "t1") {
            signal?.addEventListener("abort", () => aborted.push(true));
            releaseT1 = (v: string) => resolve(payload(v));
          } else {
            resolve(payload("task-two-logs"));
          }
        }),
    );
    const { rerender } = render(
      <TaskLogPanel missionId="m1" taskId="t1" active={true} pollMs={20} />,
    );
    await waitFor(() => expect(taskLogs).toHaveBeenCalled());
    rerender(<TaskLogPanel missionId="m1" taskId="t2" active={true} pollMs={20} />);
    await waitFor(() => expect(screen.getByText("task-two-logs")).toBeInTheDocument());
    expect(aborted).toEqual([true]);
    releaseT1("stale-task-one-logs");
    await new Promise((r) => setTimeout(r, 100));
    expect(screen.queryByText("stale-task-one-logs")).not.toBeInTheDocument();
    expect(screen.getByText("task-two-logs")).toBeInTheDocument();
  });

  it("stops polling after unmount", async () => {
    taskLogs.mockReset();
    taskLogs.mockImplementation(async () => payload("x"));
    const { unmount } = render(
      <TaskLogPanel missionId="m1" taskId="t1" active={true} pollMs={20} />,
    );
    await waitFor(() => expect(taskLogs.mock.calls.length).toBeGreaterThanOrEqual(1));
    unmount();
    const calls = taskLogs.mock.calls.length;
    await new Promise((r) => setTimeout(r, 150));
    expect(taskLogs.mock.calls.length).toBe(calls);
  });

  it("recovers after a network error", async () => {
    taskLogs.mockReset();
    taskLogs.mockRejectedValueOnce(new Error("boom"));
    taskLogs.mockImplementation(async () => payload("recovered"));
    render(<TaskLogPanel missionId="m1" taskId="t1" active={true} pollMs={20} />);
    await waitFor(() => expect(screen.getByText(/Failed to load logs/)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("recovered")).toBeInTheDocument());
  });
});
