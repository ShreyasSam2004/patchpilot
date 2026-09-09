/** Typed API client. Requests go to /api and Next rewrites them to the backend. */

import type {
  Benchmark,
  BenchmarkTask,
  BenchmarkTaskDetail,
  Diff,
  Fixture,
  Hypothesis,
  Leaderboard,
  RunDetail,
  RunEvent,
  RunFiles,
  RunList,
  RunStats,
  SystemStatus,
  TestRun,
  ToolCall,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      0,
      "Cannot reach the PatchPilot API. Is the backend running on port 8000?",
    );
  }

  if (!response.ok) {
    let detail: unknown;
    let message = `${response.status} ${response.statusText}`;
    try {
      detail = await response.json();
      const body = detail as { detail?: unknown };
      if (typeof body.detail === "string") {
        message = body.detail;
      } else if (Array.isArray(body.detail)) {
        // FastAPI validation errors.
        message = body.detail
          .map((e: { loc?: unknown[]; msg?: string }) =>
            `${(e.loc ?? []).slice(1).join(".")}: ${e.msg ?? ""}`.trim(),
          )
          .join("; ");
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, message, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface CreateRunInput {
  issue: string;
  repo_url?: string | null;
  local_path?: string | null;
  fixture?: string | null;
  repository?: string | null;
  base_commit?: string | null;
  issue_url?: string | null;
  issue_title?: string | null;
  test_command?: string | null;
  provider?: string | null;
  model?: string | null;
  sandbox?: string | null;
  max_iterations?: number | null;
  max_tool_calls?: number | null;
  max_runtime_s?: number | null;
}

export interface CreateBenchmarkInput {
  name?: string | null;
  dataset?: string | null;
  split?: string | null;
  limit?: number | null;
  instance_ids?: string[] | null;
  offset?: number;
  provider?: string | null;
  model?: string | null;
  sandbox?: string | null;
  max_iterations?: number | null;
  evaluate?: boolean;
}

function clean<T extends object>(input: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(input).filter(([, v]) => v !== null && v !== undefined && v !== ""),
  ) as Partial<T>;
}

export const api = {
  // -- system
  status: () => request<SystemStatus>("/api/status"),
  stats: () => request<RunStats>("/api/stats"),
  config: () => request<Record<string, unknown>>("/api/config"),
  health: () => request<{ status: string; version: string }>("/api/health"),

  // -- runs
  createRun: (input: CreateRunInput) =>
    request<RunDetail>("/api/runs", {
      method: "POST",
      body: JSON.stringify(clean(input)),
    }),
  listRuns: (params: { limit?: number; offset?: number; state?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    if (params.state) query.set("state", params.state);
    const suffix = query.toString();
    return request<RunList>(`/api/runs${suffix ? `?${suffix}` : ""}`);
  },
  getRun: (id: string) => request<RunDetail>(`/api/runs/${id}`),
  getEvents: (id: string, after = 0) =>
    request<RunEvent[]>(`/api/runs/${id}/events?after=${after}`),
  getDiff: (id: string) => request<Diff>(`/api/runs/${id}/diff`),
  getTests: (id: string) => request<TestRun[]>(`/api/runs/${id}/tests`),
  getTools: (id: string) => request<ToolCall[]>(`/api/runs/${id}/tools`),
  getHypotheses: (id: string) => request<Hypothesis[]>(`/api/runs/${id}/hypotheses`),
  getFiles: (id: string) => request<RunFiles>(`/api/runs/${id}/files`),
  cancelRun: (id: string) =>
    request<{ cancel_requested: boolean }>(`/api/runs/${id}/cancel`, { method: "POST" }),
  fixtures: () => request<Fixture[]>("/api/runs/fixtures"),

  // -- benchmarks
  createBenchmark: (input: CreateBenchmarkInput) =>
    request<Benchmark>("/api/benchmarks", {
      method: "POST",
      body: JSON.stringify(clean(input)),
    }),
  listBenchmarks: () =>
    request<{ items: Benchmark[]; total: number }>("/api/benchmarks"),
  getBenchmark: (id: string) => request<Benchmark>(`/api/benchmarks/${id}`),
  getBenchmarkTasks: (id: string) =>
    request<BenchmarkTask[]>(`/api/benchmarks/${id}/tasks`),
  getBenchmarkTask: (id: string, taskId: string) =>
    request<BenchmarkTaskDetail>(`/api/benchmarks/${id}/tasks/${taskId}`),
  getPredictions: (id: string) =>
    request<{ instance_id: string; model_name_or_path: string; model_patch: string }[]>(
      `/api/benchmarks/${id}/predictions`,
    ),
  evaluateBenchmark: (id: string) =>
    request<{ queued: boolean }>(`/api/benchmarks/${id}/evaluate`, { method: "POST" }),
  cancelBenchmark: (id: string) =>
    request<{ cancel_requested: boolean }>(`/api/benchmarks/${id}/cancel`, {
      method: "POST",
    }),
  benchmarkPreflight: () => request<Record<string, unknown>>("/api/benchmarks/preflight"),
  leaderboard: () => request<Leaderboard>("/api/benchmarks/leaderboard"),

  // -- mcp
  mcpStatus: () => request<Record<string, unknown>>("/api/mcp/status"),
  mcpProbe: () => request<Record<string, unknown>[]>("/api/mcp/probe"),
  mcpTools: () =>
    request<{
      policy: { allow: string[]; deny: string[]; default: string };
      tools: SystemStatus["tools"];
      allowed_count: number;
      blocked_count: number;
    }>("/api/mcp/tools"),
};

/**
 * Subscribe to a run's live event stream.
 *
 * Resumes from `after`, so a reconnect never replays or drops events. Returns
 * an unsubscribe function.
 */
export function streamRunEvents(
  runId: string,
  after: number,
  handlers: {
    onEvent: (event: RunEvent) => void;
    onDone?: (state: string) => void;
    onError?: (error: Event) => void;
  },
): () => void {
  const source = new EventSource(`/api/runs/${runId}/stream?after=${after}`);

  source.onmessage = (message) => {
    try {
      handlers.onEvent(JSON.parse(message.data) as RunEvent);
    } catch {
      /* ignore a malformed frame rather than tearing down the stream */
    }
  };

  source.addEventListener("done", (message) => {
    try {
      const data = JSON.parse((message as MessageEvent).data) as { state: string };
      handlers.onDone?.(data.state);
    } catch {
      handlers.onDone?.("");
    }
    source.close();
  });

  source.onerror = (error) => {
    handlers.onError?.(error);
  };

  return () => source.close();
}
