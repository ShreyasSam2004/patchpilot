"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  Button,
  EmptyState,
  ErrorBanner,
  Panel,
  RunStateBadge,
  Select,
  Spinner,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { formatDuration, formatRelative } from "@/lib/format";
import { isTerminal, type RunSummary } from "@/lib/types";

const STATES = [
  "",
  "PENDING",
  "ANALYZING",
  "TESTING",
  "RESOLVED",
  "FAILED",
  "TIMEOUT",
  "CANCELLED",
];

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [state, setState] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.listRuns({ limit: 100, state: state || undefined });
      setRuns(data.items);
      setTotal(data.total);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [state]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while anything is still running, so the list stays live.
  useEffect(() => {
    if (!runs.some((run) => !isTerminal(run.state))) return;
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [runs, load]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Runs</h1>
          <p className="mt-0.5 text-[12px] text-[var(--text-dim)]">
            {total} run{total === 1 ? "" : "s"} · SWE-bench runs are listed under Results
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select
            value={state}
            onChange={(e) => setState(e.target.value)}
            className="w-44"
            aria-label="Filter by state"
          >
            {STATES.map((option) => (
              <option key={option} value={option}>
                {option || "All states"}
              </option>
            ))}
          </Select>
          <Link
            href="/runs/new"
            className="focus-ring rounded-md bg-[var(--color-accent)] px-3 py-2 text-[13px] font-medium text-white hover:bg-[var(--color-accent)]/85"
          >
            New run
          </Link>
        </div>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => void load()} />}

      <Panel>
        {loading ? (
          <Spinner />
        ) : runs.length === 0 ? (
          <EmptyState
            title="No runs yet"
            description="Start one against a built-in fixture to watch the agent investigate, patch and re-test in under a minute."
            action={
              <Link href="/runs/new">
                <Button variant="primary" size="sm">
                  Start a run
                </Button>
              </Link>
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-[var(--border)] bg-[var(--bg-raised)] text-left">
                  {["Run", "Repository", "Issue", "State", "Iter", "Tests", "Runtime", "Queued"].map(
                    (heading) => (
                      <th
                        key={heading}
                        className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--text-dim)]"
                      >
                        {heading}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.id}
                    className="border-b border-[var(--border)] transition-colors last:border-0 hover:bg-[var(--bg-input)]"
                  >
                    <td className="px-3 py-2">
                      <Link
                        href={`/runs/${run.id}`}
                        className="mono text-[12px] font-medium text-[var(--color-accent)] hover:underline"
                      >
                        #{run.code}
                      </Link>
                    </td>
                    <td className="mono max-w-[200px] truncate px-3 py-2 text-[12px] text-[var(--text-muted)]">
                      {run.repository}
                    </td>
                    <td className="max-w-[380px] truncate px-3 py-2 text-[var(--text-muted)]">
                      {run.issue_title || "—"}
                    </td>
                    <td className="px-3 py-2">
                      <RunStateBadge state={run.state} />
                    </td>
                    <td className="mono px-3 py-2 tabular-nums text-[12px] text-[var(--text-dim)]">
                      {run.metrics.iterations ?? "—"}
                    </td>
                    <td className="mono px-3 py-2 tabular-nums text-[12px] text-[var(--text-dim)]">
                      {run.metrics.test_runs ?? "—"}
                    </td>
                    <td className="mono px-3 py-2 tabular-nums text-[12px] text-[var(--text-dim)]">
                      {formatDuration(run.metrics.runtime_s)}
                    </td>
                    <td className="px-3 py-2 text-[12px] text-[var(--text-dim)]">
                      {formatRelative(run.queued_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
