import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { activityHref, describeEvent, relativeTime, unreadAttention, type ActivityEvent, type ActivityItem } from "../lib/activity";
import { useGlobalEvents } from "../lib/ws";
import { ActivityContext, useActivity } from "../lib/activityContext";

const FEED_LIMIT = 100;
const SEED_LIMIT = 50;
const LAST_SEEN_KEY = "gg-activity-seen";
const CLOCK_TICK_MS = 30_000;
const BASE_TITLE = "GG Orchestrator";
const TITLE_REFRESH_MS = 30_000;

/** Seeded rows use "+00:00" and live frames "Z": compare instants, not strings. */
function atMs(iso: string): number {
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : 0;
}

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

  const [seeded, setSeeded] = useState(false);
  const titles = useRef(new Map<string, string>());
  const lastTitleFetch = useRef(0);

  const add = useCallback((incoming: ActivityItem[]) => {
    const fresh = incoming.filter(i => !seen.current.has(i.id));
    if (!fresh.length) return;
    for (const i of fresh) {
      seen.current.add(i.id);
      if (i.missionId && i.missionTitle) titles.current.set(i.missionId, i.missionTitle);
    }
    setItems(prev => [...fresh, ...prev].sort((a, b) => atMs(b.at) - atMs(a.at)).slice(0, FEED_LIMIT));
  }, []);

  const [gap, setGap] = useState(false);
  const newestKnown = useRef(0);
  useEffect(() => { newestKnown.current = items.length ? atMs(items[0].at) : 0; }, [items]);

  const seed = useCallback(() => {
    let cancelled = false;
    const before = newestKnown.current;
    api.events.recent(SEED_LIMIT)
      .then(events => {
        if (cancelled) return;
        // A full page entirely newer than what we already had means events
        // between them may be missing from the feed: say so.
        if (before > 0 && events.length >= SEED_LIMIT && events.every(e => atMs(e.created_at) > before)) setGap(true);
        add(events.map(describeEvent).filter((i): i is ActivityItem => i !== null));
        setSeeded(true);
        setSeedError(null);
      })
      .catch((e: unknown) => { if (!cancelled) setSeedError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [add]);

  useEffect(() => seed(), [seed]);

  /** Live frames carry no mission title: reuse known titles, else refresh
   * the mission list at most once per interval. */
  const withTitle = useCallback((item: ActivityItem): ActivityItem => {
    if (!item.missionId || item.missionTitle) return item;
    const known = titles.current.get(item.missionId);
    if (known) return { ...item, missionTitle: known };
    const now = Date.now();
    if (now - lastTitleFetch.current > TITLE_REFRESH_MS) {
      lastTitleFetch.current = now;
      void api.missions.list().then(missions => {
        for (const m of missions) titles.current.set(m.id, m.title);
        setItems(prev => prev.map(i => (i.missionId && !i.missionTitle && titles.current.has(i.missionId)
          ? { ...i, missionTitle: titles.current.get(i.missionId) ?? null } : i)));
      }).catch(() => { /* title is cosmetic; the id stays shown */ });
    }
    return item;
  }, []);

  const { connected } = useGlobalEvents((event) => {
    const item = describeEvent(event as ActivityEvent);
    if (item) add([withTitle(item)]);
  }, live);

  // /ws/events does not replay: re-seed on every (re)connect so events
  // published while disconnected are not silently lost.
  const wasConnected = useRef(false);
  useEffect(() => {
    if (connected && !wasConnected.current) {
      wasConnected.current = true;
      return seed();
    }
    if (!connected) wasConnected.current = false;
  }, [connected, seed]);

  const unread = useMemo(() => unreadAttention(items, atMs(lastSeen), atMs), [items, lastSeen]);
  const markSeen = useCallback(() => {
    const newest = items[0]?.at;
    if (newest) { writeLastSeen(newest); setLastSeen(newest); }
  }, [items]);

  useEffect(() => {
    document.title = unread > 0 ? `(${unread}) ${BASE_TITLE}` : BASE_TITLE;
  }, [unread]);

  const value = useMemo(() => ({ items, unread, connected, seeded, seedError, gap, open, setOpen, markSeen }),
    [items, unread, connected, seeded, seedError, gap, open, markSeen]);
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
  const { items, connected, seeded, seedError, gap, open, setOpen, markSeen } = useActivity();
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
      {gap && <p role="status" className="notice">Some activity from while the connection was down may be missing here. Mission pages show complete history.</p>}
      {!items.length && !seedError && !seeded && <p role="status" className="muted">Loading recent activity…</p>}
      {!items.length && !seedError && seeded && <p className="muted">No activity recorded yet.</p>}
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
