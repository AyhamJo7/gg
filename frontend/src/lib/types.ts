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
  retry_of_mission_id?: string | null;
  /** Derived caveats; absent on older backends (render as unknown, not clean). */
  trust?: MissionTrust | null;
}

export type SeverityCounts = Record<"BLOCKER" | "HIGH" | "MEDIUM" | "LOW", number>;

export interface ReviewSummary {
  id: string | null;
  reviewer: string;
  independent: boolean;
  degradation_reason: string | null;
  writer_set: string[];
  reviewer_in_writer_set: boolean;
  parsed: boolean | null;
  reviewed_base_sha: string | null;
  reviewed_head_sha: string | null;
  created_at: string | null;
}

export interface MissionTrust {
  unresolved_findings: SeverityCounts;
  unresolved_other: number;
  unverified_repairs: number;
  inherited_unresolved: number;
  inherited_available: boolean;
  review: ReviewSummary | null;
  review_count: number;
}

export interface RelayBlockRef {
  block_type: string;
  block_id: string;
  included_chars: number | null;
  original_chars: number | null;
}

export interface RelayContext {
  capture_status: string | null;
  schema_version: string | null;
  prompt_chars: number | null;
  estimated_prompt_tokens: number | null;
  block_count: number;
  warnings: string[];
  thin_evidence_blocks: RelayBlockRef[];
  truncated_blocks: RelayBlockRef[];
}

export type RelayFlag = "THIN_REQUIRED_EVIDENCE" | "CONTEXT_TRUNCATED" | "LOST_WORK" | "CONTEXT_NOT_CAPTURED";

export interface RelayRun {
  kind: "run";
  id: string;
  at: string | null;
  provider: string;
  role: string;
  stage: string | null;
  task_id: string | null;
  attempt_number: number | null;
  retry_of_run_id: string | null;
  outcome: string;
  outcome_source: "run_status" | "legacy";
  failure_class: string | null;
  exit_code: number | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  model_observed: string | null;
  commit_before: string | null;
  commit_after: string | null;
  changed_commit: boolean | null;
  summary: string;
  summary_truncated: boolean;
  context: RelayContext | null;
  flags: RelayFlag[];
}

export interface RelayHandoff {
  kind: "handoff";
  id: string;
  at: string | null;
  from_provider: string | null;
  to_provider: string | null;
  role: string | null;
  git_head: string | null;
  content_chars: number;
  preview: string;
  preview_truncated: boolean;
}

export interface RelayReview extends ReviewSummary {
  kind: "review";
  at: string | null;
}

export type RelayEntry = RelayRun | RelayHandoff | RelayReview;

export interface RelayFinding {
  id: string;
  severity: string;
  category: string | null;
  file: string | null;
  status: string;
  description: string;
  recommended_fix: string;
  text_truncated: boolean;
  origin_review_id: string | null;
  origin_sha: string | null;
  resolved_review_id: string | null;
  resolved_sha: string | null;
  verified_by: string | null;
  inherited_from_mission_id: string | null;
  created_at: string | null;
  resolved_at: string | null;
}

export interface RelayProviderTotals {
  provider: string;
  runs: number;
  succeeded: number;
  not_succeeded: number;
  in_flight: number;
  known_duration_ms: number;
  unknown_duration_runs: number;
  roles: string[];
}

export interface MissionRelay {
  mission_id: string;
  timeline: RelayEntry[];
  findings: RelayFinding[];
  providers: RelayProviderTotals[];
  runs_truncated: boolean;
  inherited_findings_available?: boolean;
  limits: {
    handoff_preview_chars: number;
    thin_evidence_block_chars: number;
    lost_work_min_ms: number;
    run_limit: number;
  };
}

export interface HandoffContent {
  id: string;
  from_provider: string | null;
  to_provider: string | null;
  role: string | null;
  content: string;
  content_chars: number;
  created_at: string | null;
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
  /** Unresolved retry-lineage findings; null when history was unreadable. */
  inherited_findings?: ReviewFinding[] | null;
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
  input_sha: string | null;
  result_sha: string | null;
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
  inherited_from_mission_id?: string | null;
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
  stage?: string;
  run_status?: string;
  model_requested?: string | null;
  model_observed?: string | null;
  duration_ms?: number | null;
  mission_id?: string | null;
  task_id?: string | null;
  product_project_id?: string | null;
}

export interface ContextBlockMeta {
  block_type: string;
  block_id: string;
  source_kind: string;
  source_ref: string;
  priority: string;
  original_chars: number;
  included_chars: number;
  estimated_tokens: number;
  representation: string;
  included: boolean;
  reason: string;
  hash: string;
}

export interface RunContextManifest {
  run_id: string;
  prompt_chars: number;
  prompt_bytes: number;
  prompt_words: number;
  estimated_prompt_tokens: number | null;
  estimator_id: string;
  prompt_hash: string;
  capture_status: string;
  schema_version?: string;
  budget_estimated_tokens?: number | null;
  used_estimated_tokens?: number | null;
  remaining_estimated_tokens?: number | null;
  repeated_context_ratio?: number | null;
  warnings?: string[];
  plan_revision?: number | null;
  blocks?: ContextBlockMeta[];
}

export interface ContextAnalytics {
  by_policy: Array<{ policy: string; runs: number; avg_estimated_tokens: number | null; avg_repeated_ratio: number | null }>;
  by_role: Array<{ role: string; runs: number; avg_estimated_tokens: number | null; avg_repeated_ratio: number | null }>;
  warning_counts: Record<string, number>;
  legacy_avg_estimated_tokens: number | null;
  compiled_avg_estimated_tokens: number | null;
  note: string;
}

export interface RunUsage {
  run_id: string;
  input_tokens_total: number | null;
  output_tokens_total: number | null;
  cache_read_input_tokens: number | null;
  cache_write_input_tokens: number | null;
  reasoning_output_tokens: number | null;
  native_total_tokens: number | null;
  source: string;
  completeness: string;
  observed_model: string | null;
  requested_model: string | null;
}

export interface RunDetail {
  run: ProviderRun;
  context: RunContextManifest | null;
  usage: RunUsage | null;
}

export interface UsageAnalytics {
  by_provider: Array<{
    provider: string;
    runs: number;
    runs_with_usage: number;
    complete_runs: number;
    partial_runs: number;
    unknown_runs: number;
    input_tokens: number | null;
    output_tokens: number | null;
    cache_read_tokens: number | null;
    reasoning_tokens: number | null;
  }>;
  note: string;
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
  paused?: number;
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
  paused?: number;
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

export interface ArtifactEvidence {
  candidate_sha: string | null;
  plan_revision: number;
  writers: Array<{ actor_type: string; provider: string | null; result_sha: string }>;
  writers_complete: boolean;
  review: unknown;
  verification: unknown;
  criteria: unknown;
  fresh_checkout: unknown;
  delivery_ready: boolean;
  blocking_reasons: string[];
  acceptance_state?: string | null;
  delivery_sha?: string | null;
  phase_attempts?: Array<Record<string, unknown>>;
}

export interface RepairAttempt {
  id: string;
  attempt_number: number;
  provider: string;
  provider_run_id?: string | null;
  base_sha: string;
  result_sha: string | null;
  outcome: string;
  review_reviewer: string | null;
  review_outcome: string | null;
  recheck_attempt_id: string | null;
  recheck_outcome: string | null;
  failure_signature: string | null;
}

export interface RepairCycle {
  id: string;
  project_id: string;
  trigger_type: string;
  trigger_evidence_id: string;
  trigger_sha: string;
  target_requirement_id: string | null;
  target_criterion_id: string | null;
  target_finding_id: string | null;
  classification: string;
  status: string;
  max_attempts: number;
  attempts_used: number;
  stop_reason: string | null;
  gate_hint: string | null;
  created_at: string;
  completed_at: string | null;
  attempts?: RepairAttempt[];
}
