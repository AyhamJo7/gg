import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReviewFindingsPanel } from "../components/ReviewFindingsPanel";
import type { ReviewFinding } from "../lib/types";

describe("ReviewFindingsPanel", () => {
  it("shows empty state", () => {
    render(<ReviewFindingsPanel findings={[]} />);
    expect(screen.getByText("No review findings recorded.")).toBeInTheDocument();
  });

  it("renders findings with severity badges", () => {
    const findings: ReviewFinding[] = [
      { id: "f1", mission_id: "m1", severity: "BLOCKER", category: "correctness", file: "src/main.py", description: "Null pointer", recommended_fix: "Add check", status: "open", created_at: "" },
      { id: "f2", mission_id: "m1", severity: "HIGH", category: "security", file: null, description: "Missing auth", recommended_fix: "Add middleware", status: "open", created_at: "" },
      { id: "f3", mission_id: "m1", severity: "LOW", category: "style", file: null, description: "Spacing", recommended_fix: "Run formatter", status: "resolved", created_at: "" },
    ];
    render(<ReviewFindingsPanel findings={findings} />);
    expect(screen.getByText("Null pointer")).toBeInTheDocument();
    expect(screen.getByText("Missing auth")).toBeInTheDocument();
    expect(screen.getByText("Spacing")).toBeInTheDocument();
    expect(screen.getByText("2 blocker/high")).toBeInTheDocument();
  });

  it("lists inherited unresolved findings and never calls a repair claim verified", () => {
    const inherited: ReviewFinding[] = [
      { id: "i1", mission_id: "parent", severity: "MEDIUM", category: "security", file: null, description: "Rate limit missing",
        recommended_fix: "", status: "repair_attempted", created_at: "", inherited_from_mission_id: "44cdae6dbf4d45c5" },
    ];
    render(<ReviewFindingsPanel findings={[]} inherited={inherited} />);
    expect(screen.getByText("Rate limit missing")).toBeInTheDocument();
    expect(screen.getByText(/Repair claimed · not verified/)).toHaveTextContent("inherited from 44cdae6d");
    expect(screen.getByText("1 unresolved")).toBeInTheDocument();
  });

  it("says when inherited history could not be read", () => {
    render(<ReviewFindingsPanel findings={[]} inherited={null} />);
    expect(screen.getByText("Findings inherited from earlier attempts could not be loaded.")).toBeInTheDocument();
  });
});
