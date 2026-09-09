"use client";

/**
 * Benchmark dashboard.
 *
 * The headline resolve rate is rendered only when `evaluated` is true. When the
 * official harness has not graded the batch, the panel says so explicitly
 * rather than showing 0% — an ungraded batch and a batch that scored zero are
 * very different claims.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { DiffPanel, TestsPanel, TracePanel } from "@/components/panels";
import { Timeline } from "@/components/timeline";
import {
  Badge,
  BenchmarkRunStatusBadge,
  BenchmarkStatusBadge,
  Button,
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

export default function BenchmarkPage() {
  const params = useParams<{ id: string }>();
  const benchmarkId = params?.id ?? "";

  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const [tasks, setTasks] = useState<BenchmarkTask[]>([]);
  const [selected, setSelected] = useState<BenchmarkTaskDetail | null>(null);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [tab, setTab] = useState<TaskTab>("issue");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!benchmarkId) return;
    try {
      const [b, t] = await Promise.all([
        api.getBenchmark(benchmarkId),
        api.getBenchmarkTasks(benchmarkId),
      ]);
      setBenchmark(b);
      setTasks(t);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [benchmarkId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!benchmark || ["COMPLETED", "FAILED", "CANCELLED"].includes(benchmark.status)) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [benchmark, load]);

  const openTask = async (taskId: string) => {
    setSelecting(taskId);
    setTab("issue");
    try {
      setSelected(await api.getBenchmarkTask(benchmarkId, taskId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSelecting(null);
    }
  };

  if (loading) return <Spinner label="Loading benchmark…" />;
  if (error && !benchmark) return <ErrorBanner message={error} onRetry={() => void load()} />;
  if (!benchmark) return null;

  const aggregate = benchmark.aggregate ?? {};
  const attempted = benchmark.resolved_tasks + benchmark.unresolved_tasks;
  const active = !["COMPLETED", "FAILED", "CANCELLED"].includes(benchmark.status);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <Link
              href="/benchmarks"
              className="text-[12px] text-[var(--text-dim)] hover:text-[var(--text)]"
            >
              Benchmarks
            </Link>
            <span className="text-[var(--text-dim)]">/</span>
            <BenchmarkRunStatusBadge status={benchmark.status} />
            {benchmark.evaluated ? (
              <Badge tone="ok">officially evaluated</Badge>
            ) : (
              <Badge tone="warn">not evaluated</Badge>
            )}
          </div>
          <h1 className="mt-1.5 text-lg font-semibold tracking-tight">
            {benchmark.name || benchmark.run_key}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-[var(--text-dim)]">
            <span className="mono">{benchmark.dataset}</span>
            <span>[{benchmark.split}]</span>
            <span>
              {benchmark.provider} / <span className="mono">{benchmark.model}</span>
            </span>
            <span className="mono">run_id={benchmark.run_key}</span>
            <span>{formatTimestamp(benchmark.created_at)}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {!benchmark.evaluated && !active && benchmark.completed_tasks > 0 && (
            <Button
              size="sm"
              onClick={async () => {
                await api.evaluateBenchmark(benchmark.id).catch(() => {});
                void load();
              }}
            >
              Run official evaluation
            </Button>
          )}
          {active && (
            <Button
              variant="danger"
              size="sm"
              onClick={async () => {
                await api.cancelBenchmark(benchmark.id).catch(() => {});
                void load();
              }}
            >
              Cancel
            </Button>
          )}
        </div>
      </div>

      {benchmark.error && <ErrorBanner message={benchmark.error} />}

      {/* ------------------------------------------------------- headline */}
      <Panel>
        {benchmark.evaluated ? (
          <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border)] sm:grid-cols-4 sm:divide-y-0">
            <Stat
              label="Tasks attempted"
              value={formatNumber(attempted)}
              hint={`${benchmark.total_tasks} in batch`}
            />
            <Stat label="Resolved" value={formatNumber(benchmark.resolved_tasks)} tone="ok" />
            <Stat
              label="Unresolved"
              value={formatNumber(benchmark.unresolved_tasks)}
              tone="bad"
            />
            <Stat
              label="Resolve rate"
              value={formatPercent(benchmark.resolve_rate)}
              tone="ok"
              hint="official SWE-bench evaluator"
            />
          </div>
        ) : (
          <div className="px-4 py-5">
            <p className="text-[13px] font-medium text-[var(--color-warn)]">
              This batch has not been graded by the official evaluator.
            </p>
            <p className="mt-1.5 max-w-2xl text-[12px] leading-relaxed text-[var(--text-muted)]">
              {benchmark.completed_tasks} of {benchmark.total_tasks} tasks have produced a
              patch. No resolve rate is shown, because a number that has not come from the
              official harness would not be a SWE-bench result.
            </p>
            <div className="mt-3 flex flex-wrap gap-6 text-[12px]">
              <span className="text-[var(--text-dim)]">
                Patches generated{" "}
                <span className="mono text-[var(--text)]">
                  {String(aggregate.tasks_with_patch ?? 0)}
                </span>
              </span>
              <span className="text-[var(--text-dim)]">
                Agent self-reported resolved{" "}
                <span className="mono text-[var(--text)]">
                  {String(aggregate.agent_reported_resolved ?? 0)}
                </span>{" "}
                <span className="text-[10px] uppercase">(not a benchmark verdict)</span>
              </span>
            </div>
          </div>
        )}
      </Panel>

      {/* ------------------------------------------------------ aggregates */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Run statistics" className="lg:col-span-2">
          <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border)] sm:grid-cols-4">
            <Stat label="Total runtime" value={formatDuration(Number(aggregate.total_runtime_s ?? 0))} />
            <Stat label="Avg runtime/task" value={formatDuration(Number(aggregate.avg_runtime_s ?? 0))} />
            <Stat label="Avg iterations" value={String(aggregate.avg_iterations ?? "—")} />
            <Stat label="Avg tool calls" value={String(aggregate.avg_tool_calls ?? "—")} />
            <Stat label="Avg test runs" value={String(aggregate.avg_test_runs ?? "—")} />
            <Stat label="Empty patches" value={formatNumber(benchmark.empty_patch_tasks)} />
            <Stat label="Errors" value={formatNumber(benchmark.error_tasks)} />
            <Stat
              label="Total cost"
              value={
                aggregate.total_cost_usd == null
                  ? "—"
                  : formatCost(Number(aggregate.total_cost_usd))
              }
              hint="estimated"
            />
          </div>
          <div className="border-t border-[var(--border)]">
            <Distribution tasks={tasks} />
          </div>
        </Panel>

        <Panel title="Configuration">
          <KeyValue
            rows={[
              ["Dataset", benchmark.dataset],
              ["Split", benchmark.split],
              ["Harness run_id", benchmark.run_key],
              ["Provider", benchmark.provider],
              ["Model", benchmark.model],
              ["Sandbox", String(benchmark.config.sandbox ?? "—")],
              ["Max iterations", String(benchmark.config.max_iterations ?? "—")],
              ["Max runtime", `${String(benchmark.config.max_runtime_s ?? "—")}s`],
              ["Selection", String((benchmark.config.reproduce as Record<string, unknown>)?.selection ?? "—")],
              ["Started", formatTimestamp(benchmark.started_at)],
              ["Finished", formatTimestamp(benchmark.finished_at)],
            ]}
          />
        </Panel>
      </div>

      {/* ----------------------------------------------------------- tasks */}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,440px)_minmax(0,1fr)]">
        <Panel title={`Tasks (${tasks.length})`}>
          {tasks.length === 0 ? (
            <EmptyState title="No tasks" icon="◇" />
          ) : (
            <div className="max-h-[720px] overflow-y-auto">
              <table className="w-full border-collapse text-[12px]">
                <tbody>
                  {tasks.map((task) => (
                    <tr
                      key={task.id}
                      onClick={() => void openTask(task.instance_id)}
                      className={`cursor-pointer border-b border-[var(--border)] transition-colors last:border-0 hover:bg-[var(--bg-input)] ${
                        selected?.task.instance_id === task.instance_id
                          ? "bg-[var(--bg-input)]"
                          : ""
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
                      <td className="mono px-3 py-2 text-right text-[10px] text-[var(--text-dim)]">
                        {task.metrics.iterations ?? "—"} it
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title={
            selected ? (
              <span className="mono text-[12px]">{selected.task.instance_id}</span>
            ) : (
              "Task detail"
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
              title="Select a task"
              description="Inspect its issue, the agent's tool calls, patch attempts, test results and the evaluator's verdict."
              icon="←"
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
                        "Benchmark verdict",
                        <span key="b">
                          {selected.task.benchmark_status}{" "}
                          <span className="text-[10px] uppercase text-[var(--text-dim)]">
                            (official evaluator)
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
                      <pre className="terminal mt-1 text-[var(--color-bad)]">
                        {selected.task.error}
                      </pre>
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
      </div>
    </div>
  );
}

/** Pass/fail distribution, drawn from the real task rows. */
function Distribution({ tasks }: { tasks: BenchmarkTask[] }) {
  const buckets = [
    { key: "RESOLVED", label: "resolved", colour: "var(--color-ok)" },
    { key: "UNRESOLVED", label: "unresolved", colour: "var(--color-bad)" },
    { key: "EMPTY_PATCH", label: "empty patch", colour: "var(--color-warn)" },
    { key: "ERROR", label: "error", colour: "var(--color-warn)" },
    { key: "NOT_EVALUATED", label: "not evaluated", colour: "var(--text-dim)" },
    { key: "PENDING", label: "pending", colour: "var(--border-strong)" },
    { key: "RUNNING", label: "running", colour: "var(--color-info)" },
  ]
    .map((bucket) => ({
      ...bucket,
      count: tasks.filter((t) => t.benchmark_status === bucket.key).length,
    }))
    .filter((bucket) => bucket.count > 0);

  const total = tasks.length || 1;

  return (
    <div className="px-4 py-3">
      <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
        Outcome distribution
      </p>
      <div className="mt-2 flex h-2.5 overflow-hidden rounded-full bg-[var(--bg-input)]">
        {buckets.map((bucket) => (
          <div
            key={bucket.key}
            style={{
              width: `${(bucket.count / total) * 100}%`,
              background: bucket.colour,
            }}
            title={`${bucket.label}: ${bucket.count}`}
          />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {buckets.map((bucket) => (
          <span key={bucket.key} className="flex items-center gap-1.5 text-[11px]">
            <span
              className="h-2 w-2 rounded-sm"
              style={{ background: bucket.colour }}
            />
            <span className="text-[var(--text-dim)]">{bucket.label}</span>
            <span className="mono text-[var(--text)]">{bucket.count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
