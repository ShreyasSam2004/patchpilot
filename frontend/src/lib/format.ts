/** Formatting helpers shared across views. */

import type { BenchmarkStatus, RunState } from "./types";

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${ms}ms` : formatDuration(ms / 1000);
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("en-US");
}

export function formatCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "—";
  if (usd === 0) return "$0.00";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/** Percentages are only ever rendered from real evaluated data. */
export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const delta = Math.max(0, Date.now() - then);
  const seconds = Math.floor(delta / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

type Tone = "ok" | "bad" | "warn" | "info" | "muted";

const RUN_STATE_TONE: Record<RunState, Tone> = {
  PENDING: "muted",
  PREPARING: "info",
  ANALYZING: "info",
  SEARCHING: "info",
  READING: "info",
  HYPOTHESIZING: "info",
  PATCHING: "info",
  TESTING: "info",
  REFLECTING: "warn",
  RESOLVED: "ok",
  FAILED: "bad",
  TIMEOUT: "warn",
  CANCELLED: "muted",
};

export function runStateTone(state: RunState): Tone {
  return RUN_STATE_TONE[state] ?? "muted";
}

const BENCHMARK_TONE: Record<BenchmarkStatus, Tone> = {
  PENDING: "muted",
  RUNNING: "info",
  RESOLVED: "ok",
  UNRESOLVED: "bad",
  EMPTY_PATCH: "warn",
  ERROR: "warn",
  NOT_EVALUATED: "muted",
};

export function benchmarkTone(status: BenchmarkStatus): Tone {
  return BENCHMARK_TONE[status] ?? "muted";
}

/** Human label for a benchmark verdict — "not evaluated" is never "0%". */
export function benchmarkLabel(status: BenchmarkStatus): string {
  switch (status) {
    case "NOT_EVALUATED":
      return "not evaluated";
    case "EMPTY_PATCH":
      return "empty patch";
    default:
      return status.toLowerCase();
  }
}

export function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

export function firstLine(text: string, limit = 120): string {
  const line = (text ?? "").trim().split("\n")[0] ?? "";
  return truncate(line, limit);
}
