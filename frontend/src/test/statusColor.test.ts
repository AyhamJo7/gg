import { describe, expect, it } from "vitest";
import { statusColor } from "../lib/status";

describe("statusColor", () => {
  it("maps lifecycle states", () => {
    expect(statusColor("COMPLETED")).toBe("green");
    expect(statusColor("IMPLEMENTING")).toBe("blue");
    expect(statusColor("RATE_LIMITED")).toBe("orange");
    expect(statusColor("FAILED")).toBe("red");
    expect(statusColor("WAITING_FOR_HUMAN")).toBe("purple");
    expect(statusColor("PAUSED")).toBe("yellow");
  });
  it("falls back for unknown states", () => {
    expect(statusColor("WHATEVER")).toBe("");
  });
});
