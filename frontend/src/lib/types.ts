/**
 * Wire types.
 *
 * These mirror the Pydantic DTOs in `backend/patchpilot/api/schemas.py`. They
 * are kept deliberately explicit rather than generated, so a backend change
 * that breaks the contract shows up as a TypeScript error here.
 */

export type RunState =
  | "PENDING"
  | "PREPARING"
  | "ANALYZING"
  | "SEARCHING"
  | "READING"
  | "HYPOTHESIZING"
  | "PATCHING"
  | "TESTING"
  | "REFLECTING"
  | "RESOLVED"
  | "FAILED"
  | "TIMEOUT"
  | "CANCELLED";

export const TERMINAL_STATES: RunState[] = [
  "RESOLVED",
  "FAILED",
  "TIMEOUT",
  "CANCELLED",
];

export function isTerminal(state: RunState): boolean {
  return TERMINAL_STATES.includes(state);
}

export type BenchmarkStatus =
  | "PENDING"
  | "RUNNING"
  | "RESOLVED"
  | "UNRESOLVED"
  | "EMPTY_PATCH"
  | "ERROR"
  | "NOT_EVALUATED";

export type EventType =
  | "RUN_STARTED"
  | "REPOSITORY_READY"
  | "ISSUE_ANALYZED"
  | "STATE_CHANGED"
  | "ITERATION_STARTED"
  | "MODEL_CALL_STARTED"
  | "MODEL_CALL_FINISHED"
  | "AGENT_MESSAGE"
  | "TOOL_CALLED"
  | "TOOL_RESULT"
  | "HYPOTHESIS_CREATED"
  | "PATCH_APPLIED"
  | "TEST_STARTED"
  | "TEST_FINISHED"
  | "ITERATION_COMPLETED"
  | "LOOP_DETECTED"
  | "RECOVERY_TRIGGERED"
  | "BUDGET_EXCEEDED"
  | "WARNING"
  | "RUN_RESOLVED"
  | "RUN_FAILED"
  | "RUN_CANCELLED";

export interface RunEvent {
  seq: number;
  type: EventType;
  iteration: number;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Metrics {
  iterations?: number;
  tool_calls?: number;
  model_calls?: number;
  files_inspected?: number;
  files_modified?: number;
  test_runs?: number;
  hypotheses?: number;
  patch_attempts?: number;
  runtime_s?: number;
  input_tokens?: number;
  output_tokens?: number;
  cached_tokens?: number;
  cost_usd?: number | null;
  loop_signals?: number;
}

export interface RunSummary {
  id: string;
  code: string;
  repository: string;
  issue_title: string | null;
  issue_url: string | null;
  state: RunState;
  review_state: ReviewState;
  summary: string;
  provider: string;
  model: string;
  sandbox: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  metrics: Metrics;
  files_modified: string[];
  has_patch: boolean;
  is_benchmark_task: boolean;
}

export interface RunDetail extends RunSummary {
  issue: string;
  repo_url: string | null;
  base_commit: string | null;
  test_command: string | null;
  stop_reason: string;
  failure_class: string | null;
  error: string | null;
  config: Record<string, unknown>;
  files_inspected: string[];
  final_patch: string;
  last_seq: number;
  cancel_requested: boolean;
}

export interface RunList {
  items: RunSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ToolCall {
  id: number;
  iteration: number;
  tool: string;
  origin: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  summary: string;
  error_code: string | null;
  duration_ms: number;
  created_at: string;
}

export interface TestRun {
  id: number;
  iteration: number;
  command: string;
  framework: string;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  total: number;
  green: boolean;
  timed_out: boolean;
  duration_ms: number;
  failing_tests: string[];
  output: string;
  created_at: string;
}

export interface Hypothesis {
  id: number;
  iteration: number;
  index: number;
  suspected_cause: string;
  evidence: string;
  files_involved: string[];
  proposed_fix: string;
  validation_plan: string;
  supersedes_previous: boolean;
  created_at: string;
}

export interface DiffLine {
  kind: "context" | "add" | "remove" | "meta";
  content: string;
  old_line: number | null;
  new_line: number | null;
}

export interface DiffHunk {
  header: string;
  old_start: number;
  old_count: number;
  new_start: number;
  new_count: number;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  old_path: string;
  additions: number;
  deletions: number;
  is_new: boolean;
  is_deleted: boolean;
  hunks: DiffHunk[];
}

export interface Diff {
  raw: string;
  files: DiffFile[];
  additions: number;
  deletions: number;
}

export interface RunFiles {
  inspected: { path: string; modified: boolean }[];
  modified: string[];
  inspected_count: number;
  modified_count: number;
}

export interface Benchmark {
  id: string;
  name: string;
  dataset: string;
  split: string;
  run_key: string;
  provider: string;
  model: string;
  config: Record<string, unknown>;
  status: string;
  error: string | null;
  total_tasks: number;
  completed_tasks: number;
  resolved_tasks: number;
  unresolved_tasks: number;
  error_tasks: number;
  empty_patch_tasks: number;
  evaluated: boolean;
  /** Denominator of resolve_rate. Use this rather than summing buckets, so a
   *  caption can never quote a total that disagrees with the percentage. */
  attempted_tasks: number;
  /** null until the official harness has graded the batch. Never render as 0%. */
  resolve_rate: number | null;
  aggregate: Record<string, number | null | boolean>;
  evaluator_detail: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface BenchmarkTask {
  id: string;
  instance_id: string;
  repo: string;
  base_commit: string;
  run_id: string | null;
  agent_status: RunState;
  benchmark_status: BenchmarkStatus;
  has_patch: boolean;
  metrics: Metrics;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface BenchmarkTaskDetail {
  task: BenchmarkTask & {
    problem_statement: string;
    model_patch: string;
    evaluator_report: Record<string, unknown>;
  };
  diff: Diff;
  run: RunDetail | null;
  tool_calls: ToolCall[];
  test_runs: TestRun[];
  hypotheses: Hypothesis[];
  events: RunEvent[];
}

export interface Leaderboard {
  has_results: boolean;
  runs: number;
  best: Benchmark | null;
  latest: Benchmark | null;
  resume_bullet?: string;
  note?: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  origin: string;
  server: string | null;
  allowed: boolean;
  reason: string;
  mutating: boolean;
}

export interface SystemStatus {
  version: string;
  environment: string;
  provider: {
    provider: string;
    model: string;
    ready: boolean;
    is_test_double: boolean;
    detail: string;
  };
  sandbox: {
    configured: string;
    docker_available: boolean;
    docker_detail: string;
    effective: string;
  };
  swebench: {
    harness_installed: boolean;
    docker_available: boolean;
    docker_detail: string;
    dataset_available?: boolean;
    dataset_detail?: string;
    dataset?: string;
    split?: string;
    free_disk_gb: number | null;
    ready: boolean;
    problems: string[];
  };
  tools: ToolInfo[];
  policy: { allow: string[]; deny: string[]; default: string };
  mcp: {
    enabled: boolean;
    package_installed: boolean;
    config_path: string;
    config_exists: boolean;
    servers: {
      name: string;
      transport: string;
      command: string;
      args: string[];
      url: string | null;
      enabled: boolean;
      env_keys: string[];
    }[];
    note: string;
  };
  budgets: Record<string, number>;
  database: string;
  queue_depth: number;
  warnings: string[];
}

export interface Fixture {
  id: string;
  title: string;
  issue: string;
  repository: string;
  test_command: string;
  expected_failing_tests: string[];
  expected_files: string[];
  difficulty: string;
  notes: string;
}

export interface RunStats {
  total: number;
  by_state: Record<string, number>;
  active: number;
  resolved: number;
  agent_success_rate: number | null;
}

/* ------------------------------------------------------------------ review */

/**
 * Where a run sits in human review. Distinct from `RunState`, which is what the
 * agent is doing: the agent believing it is finished is not the same claim as a
 * person having approved a specific patch for publication.
 */
export type ReviewState =
  | "RUNNING"
  | "PATCH_READY"
  | "REVISION_REQUESTED"
  | "REVISING"
  | "APPROVED"
  | "PUBLISHING"
  | "PR_CREATED"
  | "REJECTED"
  | "PUBLISH_FAILED";

export interface PatchVersion {
  version: number;
  /** SHA-256 of the exact patch. Approval binds to this. */
  digest: string;
  files: string[];
  insertions: number;
  deletions: number;
  patch_quality: string;
  verification: {
    passed?: number;
    failed?: number;
    errors?: number;
    framework?: string;
    green?: boolean;
    summary_recognized?: boolean;
    verified_by_tests?: boolean;
    command?: string;
  };
  model: string;
  iterations: number;
  root_cause: string;
  repair_summary: string;
  decision: string | null;
  reviewer_feedback: string;
  decided_at: string | null;
  decided_by: string | null;
  created_at: string;
  /** Only populated for the current version and for single-version fetches. */
  diff: string;
}

export interface Publication {
  state: ReviewState;
  provider: string;
  owner: string;
  repo: string;
  base_branch: string;
  base_commit_sha: string;
  observed_base_head: string | null;
  /** True when the base branch moved while the agent was working. */
  base_drifted: boolean;
  branch: string | null;
  commit_sha: string | null;
  pr_number: number | null;
  pr_url: string | null;
  pr_state: string | null;
  checks: {
    runs?: { name: string; status: string; conclusion: string | null; url: string | null }[];
    fetched_at?: string;
  };
  completed_steps: string[];
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReviewAuditEntry {
  action: string;
  patch_version: number | null;
  actor: string | null;
  feedback: string;
  patch_digest: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface Review {
  run_id: string;
  code: string;
  repository: string;
  repo_owner: string | null;
  repo_name: string | null;
  base_branch: string | null;
  base_commit: string | null;
  issue_title: string | null;
  issue_url: string | null;
  model: string;
  run_state: RunState;
  review_state: ReviewState;
  approved_patch_sha256: string | null;
  approved_at: string | null;
  approved_by: string | null;
  can_approve: boolean;
  can_publish: boolean;
  /** False when GitHub is unconfigured or the run has no publishable target. */
  publishing_available: boolean;
  publishing_unavailable_reason: string | null;
  current: PatchVersion | null;
  versions: PatchVersion[];
  publication: Publication | null;
  audit: ReviewAuditEntry[];
}

export interface ReviewDecisionInput {
  feedback?: string;
  reviewer?: string | null;
  /** Digest the reviewer was looking at; a stale one refuses the decision. */
  expected_digest?: string | null;
}
