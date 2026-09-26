import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { activityHref, describeEvent, relativeTime, type ActivityEvent, type ActivityItem } from "../lib/activity";
import { useGlobalEvents } from "../lib/ws";
import { ActivityContext, useActivity } from "../lib/activityContext";

const FEED_LIMIT = 100;
const SEED_LIMIT = 50;
const LAST_SEEN_KEY = "gg-activity-seen";
const CLOCK_TICK_MS = 30_000;
const BASE_TITLE = "GG Orchestrator";

function readLastSeen(): string {
  try { return localStorage.getItem(LAST_SEEN_KEY) ?? ""; } catch { return ""; }
}

function writeLastSeen(value: string) {
  try { localStorage.setItem(LAST_SEEN_KEY, value); } catch { /* per-viewer convenience only */ }
}

export function ActivityProvider({ children, live = true }: { children: ReactNode; live?: boolean }) {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [seedError, setSeedError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [lastSeen, setLastSeen] = useState(readLastSeen);
  const seen = useRef(new Set<string>());

  const add = useCallback((incoming: ActivityItem[]) => {
    const fresh = incoming.filter(i => !seen.current.has(i.id));
    if (!fresh.length) return;
    for (const i of fresh) seen.current.add(i.id);
    setItems(prev => [...fresh, ...prev].sort((a, b) => b.at.localeCompare(a.at)).slice(0, FEED_LIMIT));
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.events.recent(SEED_LIMIT)
      .then(events => { if (!cancelled) add(events.map(describeEvent).filter((i): i is ActivityItem => i !== null)); })
      .catch((e: unknown) => { if (!cancelled) setSeedError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [add]);

  const { connected } = useGlobalEvents((event) => {
    const item = describeEvent(event as ActivityEvent);
    if (item) add([item]);
  }, live);

  const unread = useMemo(() => items.filter(i => i.attention && i.at > lastSeen).length, [items, lastSeen]);
  const markSeen = useCallback(() => {
    const newest = items[0]?.at;
    if (newest) { writeLastSeen(newest); setLastSeen(newest); }
  }, [items]);

  useEffect(() => {
    document.title = unread > 0 ? `(${unread}) ${BASE_TITLE}` : BASE_TITLE;
  }, [unread]);

  const value = useMemo(() => ({ items, unread, connected, seedError, open, setOpen, markSeen }),
    [items, unread, connected, seedError, open, markSeen]);
  return <ActivityContext.Provider value={value}>{children}</ActivityContext.Provider>;
}

export function ActivityButton() {
  const { unread, connected, open, setOpen } = useActivity();
  return (
    <button className="activity-button" aria-expanded={open} aria-controls="activity-panel" onClick={() => setOpen(!open)}>
      <span className={`live-dot ${connected ? "on" : ""}`} aria-hidden />
      <span>Activity</span>
      <span className="sr-only">{connected ? "(live)" : "(not connected)"}</span>
      {unread > 0 && <span className="count" aria-label={`${unread} need attention`}>{unread}</span>}
    </button>
  );
}

export function ActivityPanel() {
  const { items, connected, seedError, open, setOpen, markSeen } = useActivity();
  const [now, setNow] = useState(() => Date.now());
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), CLOCK_TICK_MS);
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => { clearInterval(id); window.removeEventListener("keydown", onKey); };
  }, [open, setOpen]);
  useEffect(() => { if (open) markSeen(); }, [open, markSeen]);
  if (!open) return null;
  return (
    <aside id="activity-panel" className="activity-panel" aria-label="Workspace activity" tabIndex={-1} ref={panelRef}>
      <header className="row spread">
        <h2>Activity</h2>
        <button className="link" onClick={() => setOpen(false)} aria-label="Close activity">✕</button>
      </header>
      <p className="muted">
        {connected ? "Live — new events appear as GG records them." : "Not connected to live updates; showing recorded history."}
      </p>
      {seedError && <p role="alert" className="notice error">Recent history could not be loaded.</p>}
      {!items.length && !seedError && <p className="muted">No activity recorded yet.</p>}
      <ol className="activity-list">
        {items.map(item => {
          const href = activityHref(item);
          const body = <>
            <span className={`activity-tone ${item.tone}`} aria-hidden />
            <span className="activity-text">
              <span>{item.text}</span>
              <span className="muted">{item.missionTitle ?? (item.missionId ? `Mission ${item.missionId.slice(0, 8)}` : "Workspace")} · {relativeTime(item.at, now)}</span>
            </span>
          </>;
          return <li key={item.id}>{href ? <Link to={href} onClick={() => setOpen(false)}>{body}</Link> : <div>{body}</div>}</li>;
        })}
      </ol>
    </aside>
  );
}
