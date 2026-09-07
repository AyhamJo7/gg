import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { IntegrationPanel } from "../components/IntegrationPanel";
import type { IntegrationRecord } from "../lib/types";

describe("IntegrationPanel", () => {
  it("shows empty state", () => {
    render(<IntegrationPanel integration={null} />);
    expect(screen.getByText("Integration has not started yet.")).toBeInTheDocument();
  });

  it("shows completed integration", () => {
    const int: IntegrationRecord = {
      id: "i1", mission_id: "m1", status: "COMPLETED",
      branch_names: "[\"branch-a\"]", conflict_files: "[]",
      merged_commit: "abc1234", started_at: "", finished_at: "",
      provider: null, summary: "integrated branch-a", created_at: "",
    };
    render(<IntegrationPanel integration={int} />);
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
    expect(screen.getByText(/abc1234/)).toBeInTheDocument();
  });

  it("shows merge conflict with files", () => {
    const int: IntegrationRecord = {
      id: "i1", mission_id: "m1", status: "MERGE_CONFLICT",
      branch_names: "[]", conflict_files: "[\"src/main.py\"]",
      merged_commit: null, started_at: "", finished_at: "",
      provider: null, summary: "conflict", created_at: "",
    };
    render(<IntegrationPanel integration={int} />);
    expect(screen.getByText("MERGE_CONFLICT")).toBeInTheDocument();
    expect(screen.getByText("src/main.py")).toBeInTheDocument();
  });
});
