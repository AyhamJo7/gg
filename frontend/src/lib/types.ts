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
  scheduling_mode: string;
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
  integrations: IntegrationRecord[];
}

export interface TaskRecord {
  id: string;
  mission_id: string;
  role: string;
  status: string;
  prompt: string;
  summary: string;
  attempts: number;
  created_at: string;
  finished_at: string | null;
  title: string;
  task_type: string;
  description: string;
  preferred_providers: string;
  assigned_provider: string | null;
  workspace_scope: string;
  resource_locks: string;
  max_attempts: number;
  priority: number;
  ready_at: string | null;
  started_at: string | null;
  provider_run_id: string | null;
  checkpoint_before: string | null;
  checkpoint_after: string | null;
  result: string;
  blocking_issue: string | null;
  dag_revision: number;
}

export interface TaskDependency {
  from_task_id: string;
  to_task_id: string;
  created_at: string;
}

export interface TaskBranch {
  id: string;
  task_id: string;
  branch_name: string;
  base_commit: string;
  worktree_path: string;
  created_at: string;
  removed_at: string | null;
}

export interface ProviderReservation {
  id: string;
  task_id: string;
  provider: string;
  reserved_at: string;
  released_at: string | null;
  run_id: string | null;
  title?: string;
}

export interface TaskLock {
  id: string;
  task_id: string;
  lock_type: string;
  resource_key: string;
  acquired_at: string;
  released_at: string | null;
  title?: string;
}

export interface MissionDag {
  mission_id: string;
  scheduling_mode: string;
  tasks: TaskRecord[];
  dependencies: TaskDependency[];
  branches: TaskBranch[];
  reservations: ProviderReservation[];
  locks: TaskLock[];
}

export interface IntegrationRecord {
  id: string;
  mission_id: string;
  status: string;
  branch_names: string;
  conflict_files: string;
  merged_commit: string | null;
  started_at: string | null;
  finished_at: string | null;
  provider: string | null;
  summary: string;
  created_at: string;
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
  mission_id: string;
  severity: string;
  category: string;
  file: string | null;
  description: string;
  recommended_fix: string;
  status: string;
  created_at: string;
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

export interface TaskLogsResponse {
  stdout: string;
  stderr: string;
  run: ProviderRun | null;
  stdout_size: number;
  stderr_size: number;
  stdout_truncated: boolean;
  stderr_truncated: boolean;
}

export const TERMINAL_TASK_STATUSES = ["COMPLETED", "FAILED", "CANCELLED", "UNVERIFIED"];

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

export interface ProductProjectSummary {
  id: string;
  name: string;
  state: string;
  acceptance_state: string;
  plan_revision: number;
  target_repo_path: string;
  blocking_reason: string | null;
  delivery_sha: string | null;
  created_at: string;
  updated_at: string;
  phase_counts: Record<string, number>;
  open_gates: number;
}

export interface PlanAcceptance {
  id: string;
  description: string;
  verify: string;
}

export interface PlanRequirement {
  id: string;
  title: string;
  description: string;
  kind: string;
  acceptance: PlanAcceptance[];
}

export interface PlanPhase {
  key: string;
  title: string;
  goal: string;
  deliverables: string[];
  tasks: string[];
  depends_on: string[];
  workspace_scopes: string[];
  suggested_providers: string[];
  acceptance: PlanAcceptance[];
  requirement_ids: string[];
  verify_commands: string[];
  human_prerequisites: string[];
  effort: string;
}

export interface PlanPrerequisite {
  key: string;
  title: string;
  what_required: string;
  why_required: string;
  human_action: string;
  where_to_provide: string;
  validation: string;
  required_vars: string[];
}

export interface ProductPlan {
  product_name: string;
  goal: string;
  users: string;
  journeys: string[];
  requirements: PlanRequirement[];
  non_functional: string[];
  assumptions: string[];
  out_of_scope: string[];
  risks: string[];
  architecture: Record<string, unknown>;
  phases: PlanPhase[];
  external_prerequisites: PlanPrerequisite[];
}

export interface ProductPhaseRow {
  id: string;
  phase_key: string;
  title: string;
  goal: string;
  status: string;
  mission_id: string | null;
  depends_on: string[];
  acceptance_json: PlanAcceptance[];
  evidence_json: Record<string, unknown>;
  attempts: number;
  blocking_issue: string | null;
}

export interface ProductGateRow {
  id: string;
  phase_id: string | null;
  mission_gate_id: string | null;
  gate_type: string;
  title: string;
  what_required: string;
  why_required: string;
  blocked_ref: string;
  completed_so_far: string;
  human_action: string;
  where_to_provide: string;
  validation: string;
  after_resolve: string;
  required_vars: string[];
  status: string;
  resolution: string | null;
}

export interface RequirementEvidenceRow {
  requirement_id: string;
  status: string;
  evidence_json: Record<string, unknown>;
}

export interface CriterionResultRow {
  criterion_id: string;
  requirement_id: string;
  status: string;
  command: string;
  exit_code: number | null;
  output_tail: string;
  sha: string;
  checked_at: string;
}

export interface AcceptanceWaiverRow {
  id: string;
  target_kind: string;
  target_id: string;
  reason: string;
  actor: string;
  plan_revision: number;
  created_at: string;
}

export interface ProductProjectDetail {
  id: string;
  name: string;
  idea: string;
  constraints_text: string;
  state: string;
  acceptance_state: string;
  auto_execute: number;
  require_plan_approval: number;
  target_repo_path: string;
  plan_revision: number;
  blocking_reason: string | null;
  delivery_sha: string | null;
  delivery_report: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  phases: ProductPhaseRow[];
  gates: ProductGateRow[];
  evidence: RequirementEvidenceRow[];
  criterion_results: CriterionResultRow[];
  waivers: AcceptanceWaiverRow[];
  plan: ProductPlan | null;
  plan_revision_count: number;
}

export interface DagTaskInput {
  id: string;
  role: string;
  title: string;
  description: string;
  preferred_providers: string;
  workspace_scope: string;
  priority?: number;
  max_attempts?: number;
}

export interface DagDependencyInput {
  from_task_id: string;
  to_task_id: string;
}
