import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WorkflowTimeline } from "../components/WorkflowTimeline";

describe("WorkflowTimeline", () => {
  it("marks completed phases before the active one", () => {
    render(<WorkflowTimeline currentPhase="IMPLEMENTING" status="IMPLEMENTING" />);
    const timeline = screen.getByTestId("timeline");
    expect(timeline.textContent).toContain("Planning");
    expect(timeline.textContent).toContain("Implementation");
    expect(timeline.querySelectorAll(".node.done").length).toBe(2); // ANALYZING + PLANNING
    expect(timeline.querySelectorAll(".node.active").length).toBe(1); // IMPLEMENTING
  });

  it("shows everything done on COMPLETED", () => {
    render(<WorkflowTimeline currentPhase="FINAL_VALIDATION" status="COMPLETED" />);
    const timeline = screen.getByTestId("timeline");
    expect(timeline.querySelectorAll(".node.done").length).toBe(6);
  });
});
