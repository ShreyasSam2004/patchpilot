"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  BenchmarkRunStatusBadge,
  Button,
  EmptyState,
  ErrorBanner,
  Field,
  Input,
  Panel,
  Select,
  Spinner,
  Stat,
  WarningBanner,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { formatPercent, formatRelative } from "@/lib/format";
import type { Benchmark, SystemStatus } from "@/lib/types";

interface Preflight {
  ready?: boolean;
  problems?: string[];
  harness_installed?: boolean;
  docker_available?: boolean;
  dataset_available?: boolean;
  dataset?: string;
  free_disk_gb?: number | null;
}

export default function BenchmarksPage() {
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: "",
    limit: "5",
    offset: "0",
    instance_ids: "",
    model: "",
    max_iterations: "",
    evaluate: "true",
  });

  const load = useCallback(async () => {
    try {
      const [list, pre, sys] = await Promise.all([
        api.listBenchmarks(),
        api.benchmarkPreflight() as Promise<Preflight>,
        api.status(),
      ]);
      setBenchmarks(list.items);
      setPreflight(pre);
      setStatus(sys);
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

  useEffect(() => {
    const active = benchmarks.some(
      (b) => !["COMPLETED", "FAILED", "CANCELLED"].includes(b.status),
    );
    if (!active) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [benchmarks, load]);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setCreating(true);
    setError(null);
    const ids = form.instance_ids
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      const created = await api.createBenchmark({
        name: form.name || null,
        limit: ids.length > 0 ? null : Number(form.limit),
        offset: Number(form.offset) || 0,
        instance_ids: ids.length > 0 ? ids : null,
        model: form.model || null,
        max_iterations: form.max_iterations ? Number(form.max_iterations) : null,
        evaluate: form.evaluate === "true",
      });
      setShowForm(false);
      await load();
      window.location.href = `/benchmarks/${created.id}`;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const graded = benchmarks.filter((b) => b.evaluated && b.resolve_rate !== null);
  const best = graded.reduce<Benchmark | null>(
    (acc, b) => (acc === null || (b.resolve_rate ?? 0) > (acc.resolve_rate ?? 0) ? b : acc),
    null,
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">SWE-bench Lite</h1>
          <p className="mt-0.5 max-w-2xl text-[12px] text-[var(--text-muted)]">
            Every reported resolve rate comes from the official SWE-bench evaluator. The
            agent&apos;s own verdict is stored separately and never counted as a benchmark
            result.
          </p>
        </div>
        <Button variant="primary" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Cancel" : "New benchmark run"}
        </Button>
      </div>

      {error && <ErrorBanner message={error} onRetry={() => void load()} />}

      <WarningBanner
        items={[
          ...(status?.provider.is_test_double
            ? [
                "The provider is the scripted test double — a benchmark run would not exercise a real model.",
              ]
            : []),
          ...(preflight && !preflight.ready
            ? [
                ...(preflight.problems ?? []),
                "Patches can still be generated; they will be recorded as NOT EVALUATED until the official harness can grade them.",
              ]
            : []),
        ]}
      />

      {showForm && (
        <Panel title="Configure a benchmark batch">
          <form onSubmit={create} className="grid gap-4 p-4 sm:grid-cols-2">
            <Field label="Name" hint="optional">
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="lite-first-10"
              />
            </Field>
            <Field label="Model" hint={`default: ${status?.provider.model ?? "—"}`}>
              <Input
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
                placeholder={status?.provider.model ?? "claude-sonnet-5"}
                className="mono"
              />
            </Field>
            <Field label="Instances" hint="how many to attempt">
              <Input
                type="number"
                min={1}
                max={300}
                value={form.limit}
                onChange={(e) => setForm({ ...form, limit: e.target.value })}
                disabled={form.instance_ids.trim().length > 0}
              />
            </Field>
            <Field label="Offset" hint="deterministic slice of the sorted dataset">
              <Input
                type="number"
                min={0}
                value={form.offset}
                onChange={(e) => setForm({ ...form, offset: e.target.value })}
                disabled={form.instance_ids.trim().length > 0}
              />
            </Field>
            <Field
              label="Explicit instance ids"
              hint="optional — overrides limit/offset"
            >
              <Input
                value={form.instance_ids}
                onChange={(e) => setForm({ ...form, instance_ids: e.target.value })}
                placeholder="astropy__astropy-12907 django__django-11099"
                className="mono"
              />
            </Field>
            <Field label="Max iterations per task">
              <Input
                type="number"
                min={1}
                max={200}
                value={form.max_iterations}
                onChange={(e) => setForm({ ...form, max_iterations: e.target.value })}
                placeholder={String(status?.budgets.max_iterations ?? 30)}
              />
            </Field>
            <Field
              label="Official evaluation"
              hint={preflight?.ready ? "harness available" : "harness unavailable here"}
            >
              <Select
                value={form.evaluate}
                onChange={(e) => setForm({ ...form, evaluate: e.target.value })}
              >
                <option value="true">Run the official evaluator afterwards</option>
                <option value="false">Generate patches only</option>
              </Select>
            </Field>
            <div className="flex items-end sm:col-span-2">
              <Button type="submit" variant="primary" disabled={creating}>
                {creating ? "Queuing…" : "Queue benchmark"}
              </Button>
            </div>
          </form>
        </Panel>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <Panel title="Benchmark runs">
          {loading ? (
            <Spinner />
          ) : benchmarks.length === 0 ? (
            <EmptyState
              title="No benchmark runs yet"
              description="Queue a small batch to produce a genuine, evaluator-graded resolve rate. Nothing is displayed until the official harness has graded it."
              action={
                <Button variant="primary" size="sm" onClick={() => setShowForm(true)}>
                  Configure a batch
                </Button>
              }
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-[var(--border)] bg-[var(--bg-raised)] text-left">
                    {["Name", "Model", "Status", "Tasks", "Resolved", "Resolve rate", "Created"].map(
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
                  {benchmarks.map((benchmark) => (
                    <tr
                      key={benchmark.id}
                      className="border-b border-[var(--border)] transition-colors last:border-0 hover:bg-[var(--bg-input)]"
                    >
                      <td className="px-3 py-2">
                        <Link
                          href={`/benchmarks/${benchmark.id}`}
                          className="font-medium text-[var(--color-accent)] hover:underline"
                        >
                          {benchmark.name || benchmark.run_key}
                        </Link>
                        <div className="mono text-[10px] text-[var(--text-dim)]">
                          {benchmark.run_key}
                        </div>
                      </td>
                      <td className="mono px-3 py-2 text-[12px] text-[var(--text-muted)]">
                        {benchmark.model}
                      </td>
                      <td className="px-3 py-2">
                        <BenchmarkRunStatusBadge status={benchmark.status} />
                      </td>
                      <td className="mono px-3 py-2 tabular-nums text-[12px] text-[var(--text-dim)]">
                        {benchmark.completed_tasks}/{benchmark.total_tasks}
                      </td>
                      <td className="mono px-3 py-2 tabular-nums text-[12px]">
                        {benchmark.evaluated ? (
                          <span className="text-[var(--color-ok)]">
                            {benchmark.resolved_tasks}
                          </span>
                        ) : (
                          <span className="text-[var(--text-dim)]">—</span>
                        )}
                      </td>
                      <td className="mono px-3 py-2 tabular-nums text-[12px]">
                        {benchmark.resolve_rate === null ? (
                          <span
                            className="text-[var(--text-dim)]"
                            title="The official evaluator has not graded this batch"
                          >
                            not evaluated
                          </span>
                        ) : (
                          <span className="font-semibold text-[var(--color-ok)]">
                            {formatPercent(benchmark.resolve_rate)}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-[12px] text-[var(--text-dim)]">
                        {formatRelative(benchmark.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <div className="space-y-4">
          <Panel title="Best evaluated result">
            {best ? (
              <>
                <Stat
                  label="Resolve rate"
                  value={formatPercent(best.resolve_rate)}
                  tone="ok"
                  hint={`${best.resolved_tasks} of ${
                    best.resolved_tasks + best.unresolved_tasks
                  } graded · ${best.model}`}
                />
                <div className="border-t border-[var(--border)] px-4 py-3">
                  <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
                    Resume bullet
                  </p>
                  <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--text-muted)]">
                    Built PatchPilot, an autonomous software-engineering agent using LLM
                    tool-calling, MCP-compatible repository tools, iterative test-feedback
                    loops, and isolated code execution; resolved{" "}
                    <strong className="text-[var(--color-ok)]">
                      {formatPercent(best.resolve_rate)}
                    </strong>{" "}
                    of {best.attempted_tasks} attempted SWE-bench
                    Lite tasks ({best.model}), graded by the official SWE-bench harness.
                  </p>
                </div>
              </>
            ) : (
              <p className="px-4 py-5 text-[12px] leading-relaxed text-[var(--text-dim)]">
                No batch has been graded by the official evaluator yet. This panel stays
                empty rather than showing an estimate.
              </p>
            )}
          </Panel>

          <Panel title="Evaluation requirements">
            <ul className="divide-y divide-[var(--border)]">
              <Requirement
                ok={preflight?.dataset_available}
                label="Dataset"
                detail={preflight?.dataset ?? "princeton-nlp/SWE-bench_Lite"}
              />
              <Requirement
                ok={preflight?.harness_installed}
                label="Official harness"
                detail="swebench package"
              />
              <Requirement
                ok={preflight?.docker_available}
                label="Docker daemon"
                detail="required by the harness"
              />
              <Requirement
                ok={
                  preflight?.free_disk_gb === null ||
                  preflight?.free_disk_gb === undefined ||
                  preflight.free_disk_gb >= 50
                }
                label="Disk space"
                detail={
                  preflight?.free_disk_gb != null
                    ? `${preflight.free_disk_gb} GB free · ~120 GB recommended`
                    : "unknown"
                }
              />
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function Requirement({
  ok,
  label,
  detail,
}: {
  ok?: boolean;
  label: string;
  detail: string;
}) {
  return (
    <li className="flex items-start gap-3 px-4 py-2.5">
      <span
        className={`mono mt-0.5 text-[12px] ${
          ok ? "text-[var(--color-ok)]" : "text-[var(--color-warn)]"
        }`}
      >
        {ok ? "✓" : "○"}
      </span>
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-[var(--text)]">{label}</div>
        <div className="mono text-[11px] text-[var(--text-dim)]">{detail}</div>
      </div>
    </li>
  );
}
