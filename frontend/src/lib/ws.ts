import { useEffect, useRef, useState } from "react";
import { getAuthToken, isTauri } from "./auth";
import type { OrchestratorEvent } from "./types";

const MAX_TERMINAL_LINES = 2000;
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 15_000;

export interface TerminalLine {
  id: number;
  provider: string;
  text: string;
  ts: string;
}

interface SeenRef {
  ids: Set<string>;
  order: string[];
}

/** Bounded id set for replay dedupe (server replays history on connect). */
function makeSeen(): SeenRef {
  return { ids: new Set(), order: [] };
}

function seenAdd(seen: SeenRef, id: string): boolean {
  if (seen.ids.has(id)) return false;
  seen.ids.add(id);
  seen.order.push(id);
  if (seen.order.length > 5000) {
    const drop = seen.order.splice(0, seen.order.length - 5000);
    for (const d of drop) seen.ids.delete(d);
  }
  return true;
}

interface SocketHandlers {
  onMessage: (event: OrchestratorEvent) => void;
  onConnectedChange: (connected: boolean) => void;
}

/** Open an authenticated WebSocket with automatic reconnect.
 *
 * - bounded exponential backoff with jitter, single reconnect timer
 * - returns a disposer: socket closed, timer cancelled, no reconnect after
 */
function openReconnectingSocket(path: string, handlers: SocketHandlers): () => void {
  let disposed = false;
  let ws: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;

  const scheduleReconnect = () => {
    if (disposed) return;
    handlers.onConnectedChange(false);
    if (reconnectTimer !== null) return; // single timer invariant
    attempt += 1;
    const backoff = Math.min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_MAX_MS);
    const jitter = Math.random() * backoff * 0.25;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      void connect();
    }, backoff + jitter);
  };

  const connect = async () => {
    if (disposed) return;
    const defaultWsBase = isTauri
      ? "ws://127.0.0.1:8787"
      : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;
    const wsBase = (import.meta.env.VITE_WS_BASE as string | undefined) ?? defaultWsBase;
    // WebSocket has no Authorization-header mechanism from the browser
    // API, so the token travels as a WS subprotocol instead (see
    // backend/src/orchestrator/api/app.py's WS routes, which check it
    // before accept() since the pre-existing Origin check is trivially
    // bypassed by any non-browser client that omits Origin). A query
    // param would land in the server's access-log request line on every
    // (re)connect; the subprotocol handshake header does not.
    const token = await getAuthToken();
    if (disposed) return; // unmounted while awaiting the token
    ws = new WebSocket(`${wsBase}${path}`, token ? [token] : undefined);
    ws.onopen = () => {
      if (disposed) return;
      attempt = 0;
      handlers.onConnectedChange(true);
    };
    ws.onmessage = (msg) => {
      if (disposed) return;
      try {
        handlers.onMessage(JSON.parse(msg.data) as OrchestratorEvent);
      } catch {
        // malformed frame — ignore
      }
    };
    ws.onclose = scheduleReconnect;
    ws.onerror = () => ws?.close(); // let onclose drive the single reconnect path
  };

  void connect();

  return () => {
    disposed = true;
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws) {
      ws.onclose = null; // do not schedule reconnect after disposal
      ws.close();
      ws = null;
    }
    handlers.onConnectedChange(false);
  };
}

/** Subscribes to the mission WebSocket with automatic reconnect.
 *
 * - server replays durable history + bounded terminal tail on (re)connect;
 *   event-id dedupe makes replay idempotent (no duplicate renders)
 * - cleanup on unmount: socket closed, timer cancelled, no socket leaks
 */
export function useMissionEvents(missionId: string | null) {
  const [terminal, setTerminal] = useState<TerminalLine[]>([]);
  const [events, setEvents] = useState<OrchestratorEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const lineIdRef = useRef(0);
  const seenRef = useRef<SeenRef>(makeSeen());

  useEffect(() => {
    if (!missionId) return;
    setTerminal([]);
    setEvents([]);
    setConnected(false);
    seenRef.current = makeSeen();
    return openReconnectingSocket(`/ws/missions/${missionId}`, {
      onConnectedChange: setConnected,
      onMessage: (event) => {
        if (event.id && !seenAdd(seenRef.current, event.id)) return; // replay duplicate
        setEvents((prev) => [...prev.slice(-499), event]);
        if (event.type === "PROVIDER_OUTPUT") {
          const line: TerminalLine = {
            id: ++lineIdRef.current,
            provider: String(event.payload.provider ?? "?"),
            text: String(event.payload.line ?? ""),
            ts: event.created_at,
          };
          setTerminal((prev) => [...prev.slice(-MAX_TERMINAL_LINES), line]);
        }
      },
    });
  }, [missionId]);

  return { terminal, events, connected };
}

/** Live workspace-wide events (no replay: seed history via REST).
 * Transient provider output is dropped here; it belongs to mission views. */
export function useGlobalEvents(onEvent: (event: OrchestratorEvent) => void, enabled = true) {
  const [connected, setConnected] = useState(false);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;
  useEffect(() => {
    if (!enabled) return;
    return openReconnectingSocket("/ws/events", {
      onConnectedChange: setConnected,
      onMessage: (event) => {
        if (event.type !== "PROVIDER_OUTPUT") handlerRef.current(event);
      },
    });
  }, [enabled]);
  return { connected };
}
