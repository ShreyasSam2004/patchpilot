"use client";

/**
 * Results: the latest SWE-bench Lite batch, one row per issue.
 *
 * Read-only by design. Batches are started from the CLI (`patchpilot bench
 * run`); this page reports what the official SWE-bench harness decided. The
 * resolve rate appears only once that harness has graded the batch -- an
 * ungraded batch and a batch that scored zero are very different claims.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { DiffPanel, TestsPanel, TracePanel } from "@/components/panels";
import { Timeline } from "@/components/timeline";
import {
  Badge,
  BenchmarkRunStatusBadge,
  BenchmarkStatusBadge,
  EmptyState,
  ErrorBanner,
  KeyValue,
  Panel,
  RunStateBadge,
  Spinner,
  Stat,
  Tabs,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import {
  formatCost,
  formatDuration,
  formatNumber,
  formatPercent,
  formatTimestamp,
} from "@/lib/format";
import type { Benchmark, BenchmarkTask, BenchmarkTaskDetail } from "@/lib/types";

type TaskTab = "issue" | "patch" | "tests" | "trace" | "timeline";

const FINISHED = ["COMPLETED", "FAILED", "CANCELLED"];

export default function ResultsPage() {
  const [batch, setBatch] = useState<Benchmark | null>(null);
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [selected, setSelected] = useState<BenchmarkTaskDetail | null>(null);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [tab, setTab] = useState<TaskTab>("issue");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      // Newest first, so the first item is the batch these results describe.
      const latest = (await api.listBenchmarks()).items[0] ?? null;
      setBatch(latest);
      setTasks(latest ? await api.getBenchmarkTasks(latest.id) : []);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Follow a batch that is still running, so the table fills in live.
  useEffect(() => {
    if (!batch || FINISHED.includes(batch.status)) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [batch, load]);

  const openTask = async (instanceId: string) => {
    if (!batch) return;
    setSelecting(instanceId);
    setTab("issue");
    try {
      setSelected(await api.getBenchmarkTask(batch.id, instanceId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSelecting(null);
    }
  };

  if (loading) return <Spinner label="Loading results…" />;
  if (error && !batch) return <ErrorBanner message={error} onRetry={() => void load()} />;
  if (!batch) {
    return (
      <div className="space-y-4">
        <h1 className="text-xl font-semibold tracking-tight">Results</h1>
        <Panel>
          <EmptyState
            title="No results yet"
            description="Run a SWE-bench Lite batch from the backend with `patchpilot bench run`. Its results appear here, graded by the official SWE-bench harness."
            icon="◇"
          />
        </Panel>
      </div>
    );
  }

  const aggregate = batch.aggregate ?? {};
  const running = !FINISHED.includes(batch.status);
  const done = tasks.filter((t) => !["PENDING", "RUNNING"].includes(t.benchmark_status)).length;
  const totalCost = aggregate.total_cost_usd == null ? null : Number(aggregate.total_cost_usd);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <h1 className="text-xl font-semibold tracking-tight">Results</h1>
            {running && <BenchmarkRunStatusBadge status={batch.status} />}
            {batch.evaluated ? (
              <Badge tone="ok">graded by the official harness</Badge>
            ) : (
              <Badge tone="warn">not graded yet</Badge>
            )}
          </div>
          <p className="mt-1 text-[12px] text-[var(--text-dim)]">
            SWE-bench Lite · {batch.total_tasks} issues ·{" "}
            <span className="mono">{batch.model}</span> · {formatTimestamp(batch.created_at)}
          </p>
        </div>
      </div>

      {batch.error && <ErrorBanner message={batch.error} />}

      {/* ------------------------------------------------------- headline */}
      <Panel>
        {batch.evaluated ? (
          <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border)] sm:grid-cols-5 sm:divide-y-0">
            <Stat
              label="Resolve rate"
              value={formatPercent(batch.resolve_rate)}
              tone="ok"
              hint="official SWE-bench evaluator"
            />
            <Stat
              label="Resolved"
              value={formatNumber(batch.resolved_tasks)}
              tone="ok"
              hint={`of ${batch.attempted_tasks} issues`}
            />
            <Stat label="Unresolved" value={formatNumber(batch.unresolved_tasks)} tone="bad" />
            <Stat
              label="No patch"
              value={formatNumber(batch.empty_patch_tasks + batch.error_tasks)}
              hint="ran out of budget or failed"
            />
            <Stat
              label="Total cost"
              value={totalCost == null ? "—" : formatCost(totalCost)}
              hint="estimated model spend"
            />
          </div>
        ) : (
          <div className="px-4 py-5">
            <p className="text-[13px] font-medium text-[var(--color-warn)]">
              {running
                ? `In progress: ${done} of ${batch.total_tasks} issues finished.`
                : "This batch has not been graded by the official evaluator."}
            </p>
            <p className="mt-1.5 max-w-2xl text-[12px] leading-relaxed text-[var(--text-muted)]">
              The resolve rate appears once the official SWE-bench harness has graded every
              patch. A number that has not come from the harness would not be a SWE-bench
              result, so none is shown until then.
            </p>
          </div>
        )}
        <div className="border-t border-[var(--border)]">
          <Distribution tasks={tasks} />
        </div>
      </Panel>

      {/* ---------------------------------------------------------- table */}
      <Panel title={`Issues (${tasks.length})`}>
        {tasks.length === 0 ? (
          <EmptyState title="No issues in this batch" icon="◇" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr className="border-b border-[var(--border)] text-left text-[10px] uppercase tracking-wider text-[var(--text-dim)]">
                  <th className="px-3 py-2 font-medium">Issue</th>
                  <th className="px-2 py-2 font-medium">Official result</th>
                  <th className="px-2 py-2 font-medium">Agent said</th>
                  <th className="px-2 py-2 text-right font-medium">Iterations</th>
                  <th className="px-2 py-2 text-right font-medium">Tool calls</th>
                  <th className="px-2 py-2 text-right font-medium">Time</th>
                  <th className="px-3 py-2 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr
                    key={task.id}
                    onClick={() => void openTask(task.instance_id)}
                    className={`cursor-pointer border-b border-[var(--border)] transition-colors last:border-0 hover:bg-[var(--bg-input)] ${
                      selected?.task.instance_id === task.instance_id ? "bg-[var(--bg-input)]" : ""
                    }`}
                  >
                    <td className="mono px-3 py-2 text-[11px] text-[var(--text)]">
                      {task.instance_id}
                      {selecting === task.instance_id && (
                        <span className="ml-2 text-[var(--text-dim)]">loading…</span>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      <BenchmarkStatusBadge status={task.benchmark_status} />
                    </td>
                    <td className="px-2 py-2 text-[11px] text-[var(--text-muted)]">
                      {["PENDING", "RUNNING"].includes(task.benchmark_status)
                        ? "—"
                        : task.agent_status}
                    </td>
                    <td className="mono px-2 py-2 text-right text-[11px] text-[var(--text-muted)]">
                      {task.metrics.iterations ?? "—"}
                    </td>
                    <td className="mono px-2 py-2 text-right text-[11px] text-[var(--text-muted)]">
                      {task.metrics.tool_calls ?? "—"}
                    </td>
                    <td className="mono px-2 py-2 text-right text-[11px] text-[var(--text-muted)]">
                      {task.metrics.runtime_s == null ? "—" : formatDuration(task.metrics.runtime_s)}
                    </td>
                    <td className="mono px-3 py-2 text-right text-[11px] text-[var(--text-muted)]">
                      {task.metrics.cost_usd == null ? "—" : formatCost(task.metrics.cost_usd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* ---------------------------------------------------------- detail */}
      <Panel
        title={
          selected ? (
            <span className="mono text-[12px]">{selected.task.instance_id}</span>
          ) : (
            "Issue detail"
          )
        }
        actions={
          selected && (
            <div className="flex items-center gap-2">
              <BenchmarkStatusBadge status={selected.task.benchmark_status} />
              {selected.run && <RunStateBadge state={selected.run.state} />}
            </div>
          )
        }
      >
        {!selected ? (
          <EmptyState
            title="Select an issue above"
            description="See the issue text, the agent's patch, its test runs, every tool call, and the evaluator's verdict."
            icon="↑"
          />
        ) : (
          <>
            <Tabs<TaskTab>
              active={tab}
              onChange={setTab}
              tabs={[
                { id: "issue", label: "Issue" },
                { id: "patch", label: "Patch", count: selected.diff.files.length },
                { id: "tests", label: "Tests", count: selected.test_runs.length },
                { id: "trace", label: "Trace", count: selected.tool_calls.length },
                { id: "timeline", label: "Timeline", count: selected.events.length },
              ]}
            />
            {tab === "issue" && (
              <div className="divide-y divide-[var(--border)]">
                <KeyValue
                  rows={[
                    ["Repository", selected.task.repo],
                    ["Base commit", selected.task.base_commit],
                    [
                      "Agent verdict",
                      <span key="a" className="text-[var(--text-muted)]">
                        {selected.task.agent_status}{" "}
                        <span className="text-[10px] uppercase text-[var(--text-dim)]">
                          (self-reported)
                        </span>
                      </span>,
                    ],
                    [
                      "Official verdict",
                      <span key="b">
                        {selected.task.benchmark_status}{" "}
                        <span className="text-[10px] uppercase text-[var(--text-dim)]">
                          (SWE-bench harness)
                        </span>
                      </span>,
                    ],
                    [
                      "Agent run",
                      selected.run ? (
                        <Link
                          key="r"
                          href={`/runs/${selected.run.id}`}
                          className="text-[var(--color-accent)] hover:underline"
                        >
                          #{selected.run.code} ↗
                        </Link>
                      ) : (
                        "—"
                      ),
                    ],
                  ]}
                />
                <div className="px-4 py-3">
                  <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
                    Problem statement
                  </p>
                  <pre className="terminal mt-1.5 max-h-96 overflow-auto text-[var(--text-muted)]">
                    {selected.task.problem_statement}
                  </pre>
                </div>
                {Object.keys(selected.task.evaluator_report ?? {}).length > 0 && (
                  <div className="px-4 py-3">
                    <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
                      Evaluator report
                    </p>
                    <pre className="terminal mt-1.5 overflow-auto text-[var(--text-muted)]">
                      {JSON.stringify(selected.task.evaluator_report, null, 2)}
                    </pre>
                  </div>
                )}
                {selected.task.error && (
                  <div className="px-4 py-3">
                    <p className="text-[11px] uppercase tracking-wider text-[var(--color-bad)]">
                      Error
                    </p>
                    <pre className="terminal mt-1 text-[var(--color-bad)]">{selected.task.error}</pre>
                  </div>
                )}
              </div>
            )}
            {tab === "patch" && <DiffPanel diff={selected.diff} />}
            {tab === "tests" && <TestsPanel tests={selected.test_runs} />}
            {tab === "trace" && <TracePanel calls={selected.tool_calls} />}
            {tab === "timeline" && <Timeline events={selected.events} live={false} />}
          </>
        )}
      </Panel>

      {/* --------------------------------------------------- how it ran */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Run statistics" className="lg:col-span-2">
          <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border)] sm:grid-cols-4">
            <Stat label="Total runtime" value={formatDuration(Number(aggregate.total_runtime_s ?? 0))} />
            <Stat label="Avg time / issue" value={formatDuration(Number(aggregate.avg_runtime_s ?? 0))} />
            <Stat label="Avg iterations" value={String(aggregate.avg_iterations ?? "—")} />
            <Stat label="Avg tool calls" value={String(aggregate.avg_tool_calls ?? "—")} />
          </div>
        </Panel>
        <Panel title="Configuration">
          <KeyValue
            rows={[
              ["Dataset", `${batch.dataset} [${batch.split}]`],
              ["Model", batch.model],
              ["Sandbox", String(batch.config.sandbox ?? "—")],
              ["Max iterations", String(batch.config.max_iterations ?? "—")],
              ["Cost cap / issue", `$${String(batch.config.max_cost_usd ?? "—")}`],
              ["Harness run_id", batch.run_key],
              ["Finished", formatTimestamp(batch.finished_at)],
            ]}
          />
        </Panel>
      </div>
    </div>
  );
}

/** Outcome distribution, drawn from the real task rows. */
function Distribution({ tasks }: { tasks: BenchmarkTask[] }) {
  const buckets = [
    { key: "RESOLVED", label: "resolved", colour: "var(--color-ok)" },
    { key: "UNRESOLVED", label: "unresolved", colour: "var(--color-bad)" },
    { key: "EMPTY_PATCH", label: "no patch", colour: "var(--color-warn)" },
    { key: "ERROR", label: "error", colour: "var(--color-warn)" },
    { key: "NOT_EVALUATED", label: "awaiting grading", colour: "var(--text-dim)" },
    { key: "RUNNING", label: "running", colour: "var(--color-info)" },
    { key: "PENDING", label: "queued", colour: "var(--border-strong)" },
  ]
    .map((bucket) => ({
      ...bucket,
      count: tasks.filter((t) => t.benchmark_status === bucket.key).length,
    }))
    .filter((bucket) => bucket.count > 0);

  const total = tasks.length || 1;

  return (
    <div className="px-4 py-3">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-[var(--bg-input)]">
        {buckets.map((bucket) => (
          <div
            key={bucket.key}
            style={{ width: `${(bucket.count / total) * 100}%`, background: bucket.colour }}
            title={`${bucket.label}: ${bucket.count}`}
          />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {buckets.map((bucket) => (
          <span key={bucket.key} className="flex items-center gap-1.5 text-[11px]">
            <span className="h-2 w-2 rounded-sm" style={{ background: bucket.colour }} />
            <span className="text-[var(--text-dim)]">{bucket.label}</span>
            <span className="mono text-[var(--text)]">{bucket.count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
