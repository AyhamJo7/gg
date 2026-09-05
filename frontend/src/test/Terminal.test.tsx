import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Terminal } from "../components/Terminal";
import type { TerminalLine } from "../lib/ws";

const lines: TerminalLine[] = [
  { id: 1, provider: "claude", text: "planning the work", ts: "2024-01-01T00:00:00Z" },
  { id: 2, provider: "opencode", text: "writing files", ts: "2024-01-01T00:01:00Z" },
];

describe("Terminal", () => {
  it("renders provider-tagged lines", () => {
    render(<Terminal lines={lines} />);
    expect(screen.getByText("planning the work")).toBeInTheDocument();
    expect(screen.getByText("writing files")).toBeInTheDocument();
    expect(screen.getByText("[claude]")).toBeInTheDocument();
  });

  it("shows empty state with no lines", () => {
    render(<Terminal lines={[]} />);
    expect(screen.getByText(/No provider output yet/)).toBeInTheDocument();
  });
});
