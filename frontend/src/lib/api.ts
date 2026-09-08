/** Typed API client. All requests go through Vite's dev proxy in dev,
 * or the backend directly in the Tauri shell. */

const isTauri = typeof window !== "undefined" && ("__TAURI_INTERNALS__" in window || window.location.origin.startsWith("tauri://"));
export const BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? (isTauri ? "http://127.0.0.1:8787" : "");

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
  DagDependencyInput,
  DagTaskInput,
  GitState,
  Mission,
  MissionDag,
  MissionDetail,
  PriorityMatrix,
  Project,
  ProviderHealth,
  TaskLogsResponse,
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
      scheduling_mode: string;
      start: boolean;
    }) => req<Mission>("/api/missions", { method: "POST", body: JSON.stringify(body) }),
    action: (id: string, action: "start" | "pause" | "resume" | "cancel") =>
      req<{ status: string }>(`/api/missions/${id}/${action}`, { method: "POST" }),
    retry: (id: string) =>
      req<Mission>(`/api/missions/${id}/retry`, { method: "POST" }),
    resolveGate: (missionId: string, gateId: string, resolution: string) =>
      req<{ status: string }>(`/api/missions/${missionId}/gates/${gateId}/resolve`, {
        method: "POST",
        body: JSON.stringify({ resolution }),
      }),
    dag: (missionId: string) => req<MissionDag>(`/api/missions/${missionId}/dag`),
    updateDag: (missionId: string, body: { tasks: DagTaskInput[]; dependencies: DagDependencyInput[] }) =>
      req<{ status: string }>(`/api/missions/${missionId}/dag`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    activeTasks: (missionId: string) =>
      req<Array<Record<string, unknown>>>(`/api/missions/${missionId}/active-tasks`),
    retryTask: (missionId: string, taskId: string) =>
      req<{ status: string }>(`/api/missions/${missionId}/tasks/${taskId}/retry`, { method: "POST" }),
    cancelTask: (missionId: string, taskId: string) =>
      req<{ status: string }>(`/api/missions/${missionId}/tasks/${taskId}/cancel`, { method: "POST" }),
    taskLogs: (missionId: string, taskId: string, tailBytes = 65536, signal?: AbortSignal) =>
      req<TaskLogsResponse>(`/api/missions/${missionId}/tasks/${taskId}/logs?tail_bytes=${tailBytes}`, { signal }),
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
