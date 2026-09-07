import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TaskLogPanel } from "../components/TaskLogPanel";

vi.mock("../lib/api", () => ({
  api: {
    missions: {
      taskLogs: () => Promise.resolve({
        stdout: "hello from stdout",
        stderr: "",
        run: {
          id: "r1",
          provider: "agy",
          role: "implementation",
          failure_class: "NONE",
          provider_state: "COMPLETED",
          exit_code: 0,
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          summary: "done",
        },
      }),
    },
  },
}));

describe("TaskLogPanel", () => {
  it("shows provider and stdout", async () => {
    render(<TaskLogPanel missionId="m1" taskId="t1" />);
    await waitFor(() => expect(screen.getByText(/hello from stdout/)).toBeInTheDocument());
    expect(screen.getByText(/agy/)).toBeInTheDocument();
  });
});
