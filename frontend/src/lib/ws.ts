import { useEffect, useRef, useState } from "react";
import type { OrchestratorEvent } from "./types";

const MAX_TERMINAL_LINES = 2000;

export interface TerminalLine {
  id: number;
  provider: string;
  text: string;
  ts: string;
}

/** Subscribes to the mission WebSocket. The server replays persisted history
 * first, so a reloaded page catches up automatically. */
export function useMissionEvents(missionId: string | null) {
  const [terminal, setTerminal] = useState<TerminalLine[]>([]);
  const [events, setEvents] = useState<OrchestratorEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const idRef = useRef(0);

  useEffect(() => {
    if (!missionId) return;
    setTerminal([]);
    setEvents([]);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/missions/${missionId}`);
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as OrchestratorEvent;
        setEvents((prev) => [...prev.slice(-499), event]);
        if (event.type === "PROVIDER_OUTPUT") {
          const line: TerminalLine = {
            id: ++idRef.current,
            provider: String(event.payload.provider ?? "?"),
            text: String(event.payload.line ?? ""),
            ts: event.created_at,
          };
          setTerminal((prev) => [...prev.slice(-MAX_TERMINAL_LINES), line]);
        }
      } catch {
        // malformed frame — ignore
      }
    };
    return () => ws.close();
  }, [missionId]);

  return { terminal, events, connected };
}
