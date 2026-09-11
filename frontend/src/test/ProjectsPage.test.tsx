import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectsPage } from "../pages/ProjectsPage";
const mocks = vi.hoisted(() => ({ validate: vi.fn(), remove: vi.fn() }));
vi.mock("../lib/api", () => ({ api: { projects: {
  list: async () => [{ id: "repo", name: "Example", path: "/tmp/example" }],
  validate: mocks.validate, remove: mocks.remove,
} } }));
describe("repository controls", () => {
  beforeEach(() => { vi.clearAllMocks(); });
  it("reports validation failure instead of losing the action", async () => {
    mocks.validate.mockRejectedValue(new Error("Workspace unavailable"));
    render(<ProjectsPage />);
    expect(screen.getByLabelText("Repository path")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Validate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Workspace unavailable");
  });
  it("requires confirmation before removing a registration", async () => {
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(mocks.remove).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
