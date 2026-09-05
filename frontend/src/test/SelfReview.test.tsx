import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Badge } from "../components/Badge";
import { statusColor } from "../components/Badge";

describe("self-review disclosure", () => {
  it("SELF-REVIEW badge renders with warning color", () => {
    render(<Badge value="SELF-REVIEW" />);
    const badge = screen.getByText("SELF-REVIEW");
    expect(badge).toBeInTheDocument();
    expect(statusColor("SELF-REVIEW")).toBe("orange");
  });
});
