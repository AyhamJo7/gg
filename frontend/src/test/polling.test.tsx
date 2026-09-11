import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { usePolling } from "../lib/hooks";

describe("operator polling ownership", () => {
  it("does not overlap scheduled polls while the server is slow", async () => {
    vi.useFakeTimers();
    let finish!: (value: string) => void;
    const read = vi.fn(() => new Promise<string>(resolve => { finish = resolve; }));
    const { result, unmount } = renderHook(() => usePolling(read, 100));
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(read).toHaveBeenCalledTimes(1);
      await act(async () => finish("received"));
      expect(result.current.data).toBe("received");
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(read).toHaveBeenCalledTimes(2);
    } finally { unmount(); vi.useRealTimers(); }
  });
  it("never presents one project's late response as another project's evidence", async () => {
    let finishOld!: (value: string) => void;
    const old = new Promise<string>(resolve => { finishOld = resolve; });
    const read = vi.fn((id: string) => id === "old" ? old : Promise.resolve("new evidence"));
    const { result, rerender } = renderHook(({ id }) => usePolling(() => read(id), null, [id]), { initialProps: { id: "old" } });
    await waitFor(() => expect(read).toHaveBeenCalledWith("old"));
    rerender({ id: "new" });
    await waitFor(() => expect(result.current.data).toBe("new evidence"));
    await act(async () => finishOld("old evidence"));
    expect(result.current.data).toBe("new evidence");
  });
  it("allows recovery after a failed update without losing the last data", async () => {
    const read = vi.fn().mockResolvedValueOnce("first").mockRejectedValueOnce(new Error("offline")).mockResolvedValue("recovered");
    const { result } = renderHook(() => usePolling<string>(read, null));
    await waitFor(() => expect(result.current.data).toBe("first"));
    await act(async () => { result.current.refresh(); });
    await waitFor(() => expect(result.current.error).toBe("offline"));
    expect(result.current.data).toBe("first");
    await act(async () => { result.current.refresh(); });
    await waitFor(() => expect(result.current.data).toBe("recovered"));
    expect(result.current.error).toBeNull();
  });
});
