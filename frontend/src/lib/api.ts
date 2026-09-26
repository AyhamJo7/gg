/** Typed API client. All requests go through Vite's dev proxy in dev,
 * or the backend directly in the Tauri shell. */

import { getAuthToken, isTauri } from "./auth";

export const BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? (isTauri ? "http://127.0.0.1:8787" : "");

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  // The backend requires the bearer token on every /api/** route, GET
  // included (except /api/health) — see backend/.../api/auth.py's module
  // docstring for why GET used to be exempt and isn't anymore.
  if (path !== "/api/health") {
    const token = await getAuthToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
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
  ArtifactEvidence,
  ContextAnalytics,
  DagDependencyInput,
  DagTaskInput,
  GitState,
  HandoffContent,
  Mission,
  MissionDag,
  MissionDetail,
  MissionRelay,
  PriorityMatrix,
  ProductProjectDetail,
  ProductProjectSummary,
  Project,
  ProviderHealth,
  RepairCycle,
  RunDetail,
  TaskLogsResponse,
  UsageAnalytics,
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
    relay: (id: string) => req<MissionRelay>(`/api/missions/${encodeURIComponent(id)}/relay`),
    handoff: (id: string, handoffId: string) =>
      req<HandoffContent>(`/api/missions/${encodeURIComponent(id)}/handoffs/${encodeURIComponent(handoffId)}`),
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
    adoptChanges: (id: string, message?: string) =>
      req<{ adopted: boolean; result_sha?: string | null }>(`/api/missions/${id}/adopt-changes`, {
        method: "POST",
        body: JSON.stringify({ message: message ?? "human: adopt workspace changes" }),
      }),
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
  usageAnalytics: (productProjectId?: string) =>
    req<UsageAnalytics>(`/api/analytics/usage${productProjectId ? `?product_project_id=${productProjectId}` : ""}`),
  contextAnalytics: (productProjectId?: string) =>
    req<ContextAnalytics>(`/api/analytics/context${productProjectId ? `?product_project_id=${productProjectId}` : ""}`),
  runs: () => ({
    list: (params?: Record<string, string | number>) => {
      const q = params ? `?${new URLSearchParams(params as Record<string, string>).toString()}` : "";
      return req<{ runs: RunDetail["run"][]; next_cursor: string | null; has_more: boolean }>(`/api/runs${q}`);
    },
    get: (id: string) => req<RunDetail>(`/api/runs/${id}`),
    context: (id: string) => req<Record<string, unknown>>(`/api/runs/${id}/context`),
  }),
  health: () => req<{ status: string }>("/api/health"),
  lifecycle: {
    list: () => req<ProductProjectSummary[]>("/api/product-projects"),
    create: (body: {
      name: string;
      idea: string;
      constraints?: string;
      auto_execute?: boolean;
      require_plan_approval?: boolean;
      target_repo_path?: string;
    }) => req<ProductProjectDetail>("/api/product-projects", { method: "POST", body: JSON.stringify(body) }),
    get: (id: string) => req<ProductProjectDetail>(`/api/product-projects/${id}`),
    plan: (id: string) =>
      req<{ ok: boolean; revision?: number; plan?: unknown; errors?: string[] }>(
        `/api/product-projects/${id}/plan`,
        { method: "POST" },
      ),
    revisePlan: (id: string, plan: unknown, reason: string) =>
      req<{ ok: boolean; revision: number }>(`/api/product-projects/${id}/plan`, {
        method: "PUT",
        body: JSON.stringify({ plan, reason }),
      }),
    start: (id: string) => req<ProductProjectDetail>(`/api/product-projects/${id}/start`, { method: "POST" }),
    advance: (id: string) => req<Record<string, unknown>>(`/api/product-projects/${id}/advance`, { method: "POST" }),
    pause: (id: string) => req<{ status: string }>(`/api/product-projects/${id}/pause`, { method: "POST" }),
    resume: (id: string) => req<{ status: string }>(`/api/product-projects/${id}/resume`, { method: "POST" }),
    cancel: (id: string) => req<{ status: string }>(`/api/product-projects/${id}/cancel`, { method: "POST" }),
    retryPhase: (id: string, phaseKey: string) =>
      req<{ ok: boolean }>(`/api/product-projects/${id}/phases/${phaseKey}/retry`, { method: "POST" }),
    resolveGate: (id: string, gateId: string, resolution: string) =>
      req<{ ok: boolean }>(`/api/product-projects/${id}/gates/${gateId}/resolve`, {
        method: "POST",
        body: JSON.stringify({ resolution }),
      }),
    acceptance: (id: string, recheck = false) =>
      req<{ ok: boolean; sha?: string; findings?: string[] }>(`/api/product-projects/${id}/acceptance${recheck ? "?recheck=true" : ""}`, {
        method: "POST",
      }),
    evidence: (id: string) => req<ArtifactEvidence>(`/api/product-projects/${id}/evidence`),
    repairCycles: (id: string) =>
      req<{ cycles: RepairCycle[]; stats: Record<string, unknown> }>(`/api/product-projects/${id}/repair-cycles`),
    cancelRepairCycle: (id: string, cycleId: string) =>
      req<RepairCycle>(`/api/product-projects/${id}/repair-cycles/${cycleId}/cancel`, { method: "POST" }),
    waive: (id: string, target_kind: string, target_id: string, reason: string) =>
      req<{ ok: boolean }>(`/api/product-projects/${id}/waivers`, {
        method: "POST",
        body: JSON.stringify({ target_kind, target_id, reason }),
      }),
  },
};

export function missionWsUrl(missionId: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/missions/${missionId}`;
}
