import { createContext, useContext } from "react";
import type { ActivityItem } from "./activity";

export interface ActivityState {
  items: ActivityItem[];
  unread: number;
  connected: boolean;
  seeded: boolean;
  seedError: string | null;
  /** A reconnect re-seed could not cover the whole disconnect window. */
  gap: boolean;
  open: boolean;
  setOpen: (open: boolean) => void;
  markSeen: () => void;
}

export const ActivityContext = createContext<ActivityState | null>(null);

export function useActivity(): ActivityState {
  const ctx = useContext(ActivityContext);
  if (!ctx) throw new Error("useActivity must be used inside ActivityProvider");
  return ctx;
}
