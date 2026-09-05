/** Typed API client. All requests go through Vite's dev proxy in dev,
 * or the backend directly in the Tauri shell. */

const BASE = "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status} ${path}: ${body.slice(0, 200)}`);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

import type {
  Analytics,
  GitState,
  Mission,
  MissionDetail,
  PriorityMatrix,
  Project,
  ProviderHealth,
} from "./types";

export const api = {
  projects: {
    list: () => req<Project[]>("/api/projects"),
    add: (path: string, name?: string) =>
      req<Project>("/api/projects", { method: "POST", body: JSON.stringify({ path, name }) }),
    remove: (id: string) => req<void>(`/api/projects/${id}`, { method: "DELETE" }),
    validate: (id: string) =>
      req<{ workspace: string; project_type: string; git: Record<string, unknown> }>(
        `/api/projects/${id}/validate`,
        { method: "POST" },
      ),
    git: (id: string) => req<GitState>(`/api/projects/${id}/git`),
  },
  missions: {
    list: (projectId?: string) =>
      req<Mission[]>(`/api/missions${projectId ? `?project_id=${projectId}` : ""}`),
    get: (id: string) => req<MissionDetail>(`/api/missions/${id}`),
    create: (body: {
      project_id: string;
      title: string;
      task: string;
      autonomy: string;
      profile: string;
      start: boolean;
    }) => req<Mission>("/api/missions", { method: "POST", body: JSON.stringify(body) }),
    action: (id: string, action: "start" | "pause" | "resume" | "cancel") =>
      req<{ status: string }>(`/api/missions/${id}/${action}`, { method: "POST" }),
    resolveGate: (missionId: string, gateId: string, resolution: string) =>
      req<{ status: string }>(`/api/missions/${missionId}/gates/${gateId}/resolve`, {
        method: "POST",
        body: JSON.stringify({ resolution }),
      }),
  },
  providers: {
    list: () => req<ProviderHealth[]>("/api/providers"),
    test: (name: string) =>
      req<{ installed: boolean; path: string | null; version: string | null }>(
        `/api/providers/${name}/test`,
        { method: "POST" },
      ),
    toggle: (name: string, enabled: boolean) =>
      req<{ status: string }>(`/api/providers/${name}/toggle`, {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
  },
  settings: {
    priority: () => req<PriorityMatrix>("/api/settings/priority"),
    setPriority: (role: string, providers: string[]) =>
      req<PriorityMatrix>("/api/settings/priority", {
        method: "POST",
        body: JSON.stringify({ role, providers }),
      }),
    profiles: () => req<Record<string, PriorityMatrix>>("/api/settings/profiles"),
    saveProfile: (name: string, matrix: PriorityMatrix) =>
      req<{ status: string }>("/api/settings/profiles", {
        method: "POST",
        body: JSON.stringify({ name, matrix }),
      }),
  },
  analytics: () => req<Analytics>("/api/analytics"),
  health: () => req<{ status: string }>("/api/health"),
};

export function missionWsUrl(missionId: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/missions/${missionId}`;
}
