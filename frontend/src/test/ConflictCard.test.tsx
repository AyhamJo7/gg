import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConflictCard } from "../components/ConflictCard";

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const action = vi.fn(async (_id: string, _act: string) => ({ status: "ok" }));

vi.mock("../lib/api", () => ({
  api: {
    missions: {
      action: (id: string, act: string) => action(id, act),
    },
  },
}));

const integration = {
  id: "int-1",
  mission_id: "m1",
  status: "MERGE_CONFLICT",
  branch_names: JSON.stringify(["gg/m1/task-a", "gg/m1/task-b"]),
  conflict_files: JSON.stringify(["src/shared.txt"]),
  merged_commit: null,
  started_at: null,
  finished_at: null,
  provider: null,
  summary: "merge conflict integrating gg/m1/task-b",
  created_at: new Date().toISOString(),
};

describe("ConflictCard", () => {
  it("shows files, branches and resume action", () => {
    render(<ConflictCard integration={integration} missionId="m1" onResumed={() => {}} />);
    expect(screen.getByTestId("conflict-card")).toBeInTheDocument();
    expect(screen.getByText("src/shared.txt")).toBeInTheDocument();
    expect(screen.getByText("gg/m1/task-a")).toBeInTheDocument();
    expect(screen.getByTestId("conflict-resume")).toBeInTheDocument();
  });

  it("resumes integration and notifies", async () => {
    const onResumed = vi.fn();
    render(<ConflictCard integration={integration} missionId="m1" onResumed={onResumed} />);
    fireEvent.click(screen.getByTestId("conflict-resume"));
    await waitFor(() => expect(action).toHaveBeenCalledWith("m1", "resume"));
    await waitFor(() => expect(onResumed).toHaveBeenCalled());
  });
});
