import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ActivityButton, ActivityPanel, ActivityProvider } from "../components/ActivityCenter";
import { CommandPalette } from "../components/CommandPalette";
import { activityHref, describeEvent, relativeTime } from "../lib/activity";
import { commandMatches, commandScore } from "../lib/commands";
import type { OrchestratorEvent } from "../lib/types";

const mocks = vi.hoisted(() => ({ recent: vi.fn(), missions: vi.fn(), products: vi.fn(), onEvent: null as null | ((e: OrchestratorEvent) => void) }));
vi.mock("../lib/api", () => ({ api: {
  events: { recent: mocks.recent },
  missions: { list: mocks.missions },
  lifecycle: { list: mocks.products },
} }));
vi.mock("../lib/ws", () => ({
  useGlobalEvents: (cb: (e: OrchestratorEvent) => void) => { mocks.onEvent = cb; return { connected: true }; },
}));

const ev = (over: Partial<OrchestratorEvent> & { mission_title?: string }): OrchestratorEvent & { mission_title?: string } => ({
  id: "e1", mission_id: "m1", type: "MISSION_CREATED", payload: {}, created_at: "2026-09-12T01:00:00Z", ...over,
});

describe("describeEvent", () => {
  it("never claims independence without the recorded flag", () => {
    const item = describeEvent(ev({ type: "REVIEW_RECORDED", payload: { review_provider: "agy", independent: false, degradation_reason: "writer provenance incomplete" } }))!;
    expect(item.text).toBe("Review by agy not certified independent: writer provenance incomplete");
    expect(item.attention).toBe(true);
    expect(describeEvent(ev({ type: "REVIEW_RECORDED", payload: { review_provider: "codex", independent: true } }))!.attention).toBe(false);
  });
  it("hides routine noise and keeps unknown types readable", () => {
    expect(describeEvent(ev({ type: "LOCK_ACQUIRED" }))).toBeNull();
    expect(describeEvent(ev({ type: "SOMETHING_NEW" }))!.text).toBe("something new");
  });
  it("flags only serious findings and completions for attention", () => {
    expect(describeEvent(ev({ type: "REVIEW_FINDING_CREATED", payload: { severity: "LOW", description: "nit" } }))!.attention).toBe(false);
    expect(describeEvent(ev({ type: "REVIEW_FINDING_CREATED", payload: { severity: "HIGH", description: "bug" } }))!.attention).toBe(true);
    expect(describeEvent(ev({ type: "MISSION_COMPLETED" }))!.text).toMatch(/check its verdict/);
  });
  it("links products before missions", () => {
    expect(activityHref(describeEvent(ev({ type: "PRODUCT_DELIVERED", payload: { product_project_id: "p/1" } }))!)).toBe("/lifecycle/p%2F1");
    expect(activityHref(describeEvent(ev({}))!)).toBe("/missions?mission=m1");
  });
  it("formats relative time without inventing a time for bad input", () => {
    const now = Date.parse("2026-09-12T02:00:00Z");
    expect(relativeTime("2026-09-12T01:59:50Z", now)).toBe("just now");
    expect(relativeTime("2026-09-12T01:30:00Z", now)).toBe("30m ago");
    expect(relativeTime("garbage", now)).toBe("at an unknown time");
  });
});

describe("command matching", () => {
  it("ranks prefix over substring over subsequence", () => {
    expect(commandScore("RechnungsRadar", "rech")).toBeGreaterThan(commandScore("Pre-check", "rech"));
    expect(commandScore("Missions", "msn")).toBeGreaterThan(0);
    expect(commandScore("Missions", "zzz")).toBe(0);
  });
  it("keeps original order for ties and filters misses", () => {
    const cmds = [{ id: "a", group: "Go to", label: "Overview" }, { id: "b", group: "Go to", label: "Providers" }];
    expect(commandMatches(cmds, "").map(c => c.id)).toEqual(["a", "b"]);
    expect(commandMatches(cmds, "prov").map(c => c.id)).toEqual(["b"]);
  });
});

function Where() { return <span data-testid="where">{useLocation().pathname + useLocation().search}</span>; }

describe("CommandPalette", () => {
  beforeEach(() => {
    mocks.missions.mockResolvedValue([{ id: "rr", title: "RechnungsRadar retry", status: "COMPLETED" }]);
    mocks.products.mockResolvedValue([]);
  });
  it("opens with Ctrl+K, filters, and navigates with the keyboard", async () => {
    render(<MemoryRouter><CommandPalette staticCommands={[{ id: "nav", group: "Go to", label: "Providers", href: "/providers" }]} /><Routes><Route path="*" element={<Where />} /></Routes></MemoryRouter>);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const input = await screen.findByRole("combobox");
    await screen.findByText("RechnungsRadar retry");
    fireEvent.change(input, { target: { value: "rechn" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByTestId("where")).toHaveTextContent("/missions?mission=rr");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("runs actions and closes on Escape", async () => {
    const action = vi.fn();
    render(<MemoryRouter><CommandPalette staticCommands={[{ id: "t", group: "Preferences", label: "Switch to light mode", action }]} /></MemoryRouter>);
    act(() => { window.dispatchEvent(new Event("gg:open-palette")); });
    const input = await screen.findByRole("combobox");
    fireEvent.change(input, { target: { value: "light" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(action).toHaveBeenCalled();
    act(() => { window.dispatchEvent(new Event("gg:open-palette")); });
    fireEvent.keyDown(await screen.findByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("says when missions could not be loaded", async () => {
    mocks.missions.mockRejectedValue(new Error("down"));
    render(<MemoryRouter><CommandPalette staticCommands={[]} /></MemoryRouter>);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await waitFor(() => expect(screen.getByText(/could not be loaded/)).toBeInTheDocument());
  });
});

describe("Activity center", () => {
  beforeEach(() => { localStorage.clear(); document.title = "GG Orchestrator"; });
  it("seeds history, counts unread attention, and clears it when opened", async () => {
    mocks.recent.mockResolvedValue([
      ev({ id: "a", type: "HUMAN_GATE_CREATED", payload: { reason: "credentials" }, mission_title: "Billing" }),
      ev({ id: "b", type: "MISSION_CREATED", created_at: "2026-09-12T00:00:00Z" }),
    ]);
    render(<MemoryRouter><ActivityProvider><ActivityButton /><ActivityPanel /></ActivityProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByLabelText("1 need attention")).toBeInTheDocument());
    expect(document.title).toBe("(1) GG Orchestrator");
    fireEvent.click(screen.getByRole("button", { name: /Activity/ }));
    expect(await screen.findByText("Decision needed: credentials")).toBeInTheDocument();
    expect(screen.getByText(/Billing/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByLabelText("1 need attention")).not.toBeInTheDocument());
    expect(document.title).toBe("GG Orchestrator");
  });
  it("adds live events once and reports a failed history load", async () => {
    mocks.recent.mockRejectedValue(new Error("500"));
    render(<MemoryRouter><ActivityProvider><ActivityButton /><ActivityPanel /></ActivityProvider></MemoryRouter>);
    act(() => { mocks.onEvent?.(ev({ id: "live", type: "MISSION_FAILED", payload: { reason: "verification failed" }, created_at: "2026-09-13T00:00:00Z" })); });
    act(() => { mocks.onEvent?.(ev({ id: "live", type: "MISSION_FAILED", payload: { reason: "verification failed" }, created_at: "2026-09-13T00:00:00Z" })); });
    fireEvent.click(screen.getByRole("button", { name: /Activity/ }));
    expect(await screen.findAllByText("Mission stopped: verification failed")).toHaveLength(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Recent history could not be loaded.");
  });
});
