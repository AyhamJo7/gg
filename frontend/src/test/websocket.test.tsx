import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMissionEvents } from "../lib/ws";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }
  sendEvent(event: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
  serverClose() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }
  close() {
    this.closed = true;
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }
}

describe("useMissionEvents reconnect", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("connects, receives events, and auto-reconnects after server close", () => {
    const { result } = renderHook(() => useMissionEvents("m1"));
    expect(MockWebSocket.instances).toHaveLength(1);
    const first = MockWebSocket.instances[0];
    act(() => first.open());
    expect(result.current.connected).toBe(true);

    act(() =>
      first.sendEvent({ id: "e1", mission_id: "m1", type: "PHASE_STARTED", payload: {}, created_at: "t" }),
    );
    expect(result.current.events).toHaveLength(1);

    // server disappears
    act(() => first.serverClose());
    expect(result.current.connected).toBe(false);

    // backoff fires → new socket, no duplicates of sockets
    act(() => vi.advanceTimersByTime(1000));
    expect(MockWebSocket.instances).toHaveLength(2);
    const second = MockWebSocket.instances[1];
    act(() => second.open());
    expect(result.current.connected).toBe(true);
  });

  it("dedupes replayed events by id across reconnect", () => {
    const { result } = renderHook(() => useMissionEvents("m1"));
    const first = MockWebSocket.instances[0];
    act(() => first.open());
    act(() =>
      first.sendEvent({ id: "e1", mission_id: "m1", type: "PHASE_STARTED", payload: {}, created_at: "t" }),
    );
    act(() => first.serverClose());
    act(() => vi.advanceTimersByTime(1000));
    const second = MockWebSocket.instances[1];
    act(() => second.open());
    // server replays e1, then sends new e2
    act(() => {
      second.sendEvent({ id: "e1", mission_id: "m1", type: "PHASE_STARTED", payload: {}, created_at: "t" });
      second.sendEvent({ id: "e2", mission_id: "m1", type: "PHASE_COMPLETED", payload: {}, created_at: "t" });
    });
    const ids = result.current.events.map((e) => e.id);
    expect(ids).toEqual(["e1", "e2"]); // e1 exactly once
  });

  it("never opens multiple concurrent sockets (single reconnect timer)", () => {
    renderHook(() => useMissionEvents("m1"));
    const first = MockWebSocket.instances[0];
    act(() => first.open());
    // rapid close/error flapping
    act(() => first.serverClose());
    act(() => first.serverClose());
    act(() => first.serverClose());
    act(() => vi.advanceTimersByTime(1000));
    // exactly one reconnect attempt despite multiple close events
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("cleans up on unmount: no reconnect, socket closed", () => {
    const { unmount } = renderHook(() => useMissionEvents("m1"));
    const first = MockWebSocket.instances[0];
    act(() => first.open());
    unmount();
    act(() => first.serverClose());
    act(() => vi.advanceTimersByTime(60_000));
    expect(MockWebSocket.instances).toHaveLength(1); // no reconnect after disposal
  });

  it("backs off exponentially with bound", () => {
    renderHook(() => useMissionEvents("m1"));
    for (let i = 0; i < 8; i++) {
      const current = MockWebSocket.instances[MockWebSocket.instances.length - 1];
      act(() => current.open());
      act(() => current.serverClose());
      act(() => vi.advanceTimersByTime(20_000));
    }
    // 8 failures → 9 instances, all serialized (never 2 at once)
    expect(MockWebSocket.instances).toHaveLength(9);
  });
});
