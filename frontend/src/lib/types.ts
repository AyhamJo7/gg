/** API types mirroring the backend. */

export interface Project {
  id: string;
  name: string;
  path: string;
  detected_type: string;
  created_at: string;
}

export interface Mission {
  id: string;
  project_id: string;
  title: string;
  task: string;
  status: string;
  current_phase: string | null;
  current_provider: string | null;
  autonomy: string;
  profile: string;
  providers_used: string[];
  providers_failed: string[];
  repair_cycles: number;
  blocking_issue: string | null;
  git_head: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface ReviewRecord {
  id: string;
  mission_id: string;
  implementation_provider: string | null;
  review_provider: string;
  independent: number;
  degradation_reason: string | null;
  created_at: string;
}

export interface MissionDetail extends Mission {
  tasks: TaskRecord[];
  gates: HumanGate[];
  findings: ReviewFinding[];
  runs: ProviderRun[];
  reviews: ReviewRecord[];
  latest_review: ReviewRecord | null;
  degraded_review: boolean;
  latest_handoff: string | null;
}

export interface TaskRecord {
  id: string;
  role: string;
  status: string;
  summary: string;
  attempts: number;
  created_at: string;
  finished_at: string | null;
}

export interface HumanGate {
  id: string;
  reason: string;
  detail: string;
  choices: string[];
  recommended: string | null;
  status: string;
  resolution: string | null;
}

export interface ReviewFinding {
  id: string;
  severity: string;
  category: string;
  file: string | null;
  description: string;
  recommended_fix: string;
  status: string;
}

export interface ProviderRun {
  id: string;
  provider: string;
  role: string;
  failure_class: string;
  provider_state: string;
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
  summary: string;
}

export interface ProviderHealth {
  name: string;
  state: string;
  installed: boolean;
  executable_path: string | null;
  version: string | null;
  last_run_at: string | null;
  last_error: string | null;
  cooldown_until: string | null;
  consecutive_failures: number;
  total_runs: number;
  successful_runs: number;
  rate_limit_events: number;
  total_runtime_seconds: number;
}

export interface GitState {
  is_repo: boolean;
  branch: string;
  head: string | null;
  modified: string[];
  added: string[];
  deleted: string[];
  untracked: string[];
  diff_stat: string;
  diff: string;
  recent_commits: string[];
}

export interface OrchestratorEvent {
  id: string;
  mission_id: string | null;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Analytics {
  missions_by_status: Record<string, number>;
  provider_stats: Array<{
    provider: string;
    runs: number;
    successes: number;
    rate_limits: number;
    avg_seconds: number | null;
  }>;
  findings_by_severity: Record<string, number>;
}

export type PriorityMatrix = Record<string, string[]>;
