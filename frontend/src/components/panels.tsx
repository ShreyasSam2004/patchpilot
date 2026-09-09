"use client";

/** The FILES / PATCH / TESTS / TRACE / METRICS panels of a run workspace. */

import { useState } from "react";

import { Badge, EmptyState, Stat } from "./ui";
import { formatCost, formatDuration, formatMs, formatNumber } from "@/lib/format";
import type {
  Diff,
  Hypothesis,
  Metrics,
  RunDetail,
  RunFiles,
  TestRun,
  ToolCall,
} from "@/lib/types";

/* ------------------------------------------------------------------ FILES */

export function FilesPanel({ files }: { files: RunFiles | null }) {
  if (!files || files.inspected.length === 0) {
    return <EmptyState title="No files recorded yet" icon="◇" />;
  }
  return (
    <div>
      <div className="flex gap-6 border-b border-[var(--border)] px-4 py-2.5 text-[12px]">
        <span className="text-[var(--text-dim)]">
          Inspected <span className="mono text-[var(--text)]">{files.inspected_count}</span>
        </span>
        <span className="text-[var(--text-dim)]">
          Modified{" "}
          <span className="mono text-[var(--color-ok)]">{files.modified_count}</span>
        </span>
      </div>
      <ul className="divide-y divide-[var(--border)]">
        {files.inspected.map((file) => (
          <li key={file.path} className="flex items-center gap-3 px-4 py-1.5">
            <span
              className={`mono w-3 text-[12px] ${
                file.modified ? "text-[var(--color-ok)]" : "text-[var(--text-dim)]"
              }`}
            >
              {file.modified ? "M" : "·"}
            </span>
            <span
              className={`mono min-w-0 flex-1 truncate text-[12px] ${
                file.modified ? "text-[var(--text)]" : "text-[var(--text-muted)]"
              }`}
            >
              {file.path}
            </span>
            {file.modified && <Badge tone="ok">modified</Badge>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------------------ PATCH */

export function DiffPanel({ diff }: { diff: Diff | null }) {
  const [raw, setRaw] = useState(false);

  if (!diff || diff.files.length === 0) {
    return (
      <EmptyState
        title="No patch produced"
        description="The agent did not change any files in this run."
        icon="◇"
      />
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-2.5">
        <div className="flex items-center gap-3 text-[12px]">
          <span className="text-[var(--text-dim)]">
            {diff.files.length} file{diff.files.length === 1 ? "" : "s"} changed
          </span>
          <span className="mono text-[var(--color-ok)]">+{diff.additions}</span>
          <span className="mono text-[var(--color-bad)]">−{diff.deletions}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setRaw((v) => !v)}
            className="focus-ring rounded px-2 py-1 text-[11px] text-[var(--text-dim)] hover:text-[var(--text)]"
          >
            {raw ? "Rendered" : "Raw"}
          </button>
          <button
            onClick={() => void navigator.clipboard?.writeText(diff.raw)}
            className="focus-ring rounded px-2 py-1 text-[11px] text-[var(--text-dim)] hover:text-[var(--text)]"
          >
            Copy
          </button>
        </div>
      </div>

      {raw ? (
        <pre className="terminal max-h-[640px] overflow-auto px-4 py-3 text-[var(--text-muted)]">
          {diff.raw}
        </pre>
      ) : (
        <div className="max-h-[640px] overflow-y-auto">
          {diff.files.map((file) => (
            <div key={file.path} className="border-b border-[var(--border)] last:border-0">
              <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-[var(--border)] bg-[var(--bg-raised)] px-4 py-2">
                <span className="mono min-w-0 flex-1 truncate text-[12px] font-medium text-[var(--text)]">
                  {file.path}
                </span>
                {file.is_new && <Badge tone="ok">new</Badge>}
                {file.is_deleted && <Badge tone="bad">deleted</Badge>}
                <span className="mono text-[11px] text-[var(--color-ok)]">
                  +{file.additions}
                </span>
                <span className="mono text-[11px] text-[var(--color-bad)]">
                  −{file.deletions}
                </span>
              </div>
              {file.hunks.map((hunk, hunkIndex) => (
                <div key={hunkIndex}>
                  <div className="mono border-y border-[var(--border)] bg-[var(--bg-input)] px-4 py-1 text-[11px] text-[var(--text-dim)]">
                    {hunk.header}
                  </div>
                  {hunk.lines.map((line, lineIndex) => (
                    <div
                      key={lineIndex}
                      className={`diff-line diff-${line.kind === "meta" ? "context" : line.kind}`}
                    >
                      <span className="diff-gutter">{line.old_line ?? ""}</span>
                      <span className="diff-gutter">{line.new_line ?? ""}</span>
                      <span className="diff-content">{line.content}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ TESTS */

export function TestsPanel({ tests }: { tests: TestRun[] }) {
  const [expanded, setExpanded] = useState<number | null>(
    tests.length > 0 ? tests[tests.length - 1].id : null,
  );

  if (tests.length === 0) {
    return (
      <EmptyState
        title="No test runs yet"
        description="The agent runs the project's tests to validate every patch."
        icon="◇"
      />
    );
  }

  return (
    <div className="divide-y divide-[var(--border)]">
      {tests.map((test, index) => {
        const open = expanded === test.id;
        return (
          <div key={test.id}>
            <button
              onClick={() => setExpanded(open ? null : test.id)}
              className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-[var(--bg-input)]"
            >
              <span className="mono w-6 shrink-0 text-[11px] text-[var(--text-dim)]">
                #{index + 1}
              </span>
              <Badge tone={test.green ? "ok" : "bad"} dot>
                {test.green ? "pass" : "fail"}
              </Badge>
              <span className="mono min-w-0 flex-1 truncate text-[12px] text-[var(--text-muted)]">
                {test.command}
              </span>
              <span className="mono hidden shrink-0 gap-2 text-[11px] sm:flex">
                <span className="text-[var(--color-ok)]">{test.passed} passed</span>
                {test.failed > 0 && (
                  <span className="text-[var(--color-bad)]">{test.failed} failed</span>
                )}
                {test.errors > 0 && (
                  <span className="text-[var(--color-warn)]">{test.errors} errors</span>
                )}
              </span>
              <span className="mono shrink-0 text-[11px] text-[var(--text-dim)]">
                {formatMs(test.duration_ms)}
              </span>
              <span className="mono w-3 shrink-0 text-[11px] text-[var(--text-dim)]">
                {open ? "−" : "+"}
              </span>
            </button>

            {open && (
              <div className="border-t border-[var(--border)] bg-[var(--bg-input)]">
                {test.failing_tests.length > 0 && (
                  <div className="border-b border-[var(--border)] px-4 py-2">
                    <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
                      Failing tests
                    </p>
                    <ul className="mono mt-1 space-y-0.5 text-[11px] text-[var(--color-bad)]">
                      {test.failing_tests.map((name) => (
                        <li key={name}>{name}</li>
                      ))}
                    </ul>
                  </div>
                )}
                <pre className="terminal max-h-96 overflow-auto px-4 py-3 text-[var(--text-muted)]">
                  {test.output || "(no output captured)"}
                </pre>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ TRACE */

const TRACE_VERB: Record<string, string> = {
  list_files: "LIST",
  search_code: "SEARCH",
  find_symbol: "SYMBOL",
  read_file: "READ",
  record_hypothesis: "HYPOTHESIS",
  apply_patch: "PATCH",
  edit_file: "EDIT",
  write_file: "WRITE",
  git_diff: "DIFF",
  git_status: "STATUS",
  git_log: "LOG",
  run_tests: "TEST",
  run_command: "EXEC",
  finish: "FINISH",
};

function traceArgument(call: ToolCall): string {
  const args = call.arguments ?? {};
  for (const key of ["query", "symbol", "path", "command", "diff", "old_string"]) {
    const value = args[key];
    if (typeof value === "string" && value) {
      return value.length > 160 ? `${value.slice(0, 160)}…` : value;
    }
    if (Array.isArray(value)) return value.join(" ");
  }
  const keys = Object.keys(args);
  return keys.length > 0 ? keys.map((k) => `${k}=${String(args[k])}`).join(" ") : "";
}

export function TracePanel({ calls }: { calls: ToolCall[] }) {
  if (calls.length === 0) {
    return <EmptyState title="No tool calls recorded" icon="◇" />;
  }
  return (
    <div className="max-h-[640px] overflow-y-auto">
      <table className="w-full border-collapse">
        <tbody>
          {calls.map((call) => (
            <tr
              key={call.id}
              className="border-b border-[var(--border)] align-top last:border-0"
            >
              <td className="mono w-10 px-3 py-1.5 text-[11px] text-[var(--text-dim)]">
                {call.iteration}
              </td>
              <td className="w-24 px-1 py-1.5">
                <span
                  className={`mono text-[11px] font-semibold ${
                    call.ok ? "text-[var(--color-accent)]" : "text-[var(--color-bad)]"
                  }`}
                >
                  {TRACE_VERB[call.tool] ?? call.tool.toUpperCase()}
                </span>
              </td>
              <td className="mono px-1 py-1.5 text-[11px] text-[var(--text-muted)]">
                <div className="break-all">{traceArgument(call)}</div>
                <div className="mt-0.5 text-[var(--text-dim)]">
                  {call.ok ? "" : "ERROR: "}
                  {call.summary}
                </div>
              </td>
              <td className="mono w-16 px-3 py-1.5 text-right text-[11px] text-[var(--text-dim)]">
                {formatMs(call.duration_ms)}
              </td>
              {call.origin !== "native" && (
                <td className="px-2 py-1.5">
                  <Badge tone="accent">mcp</Badge>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------------------------------------------------------- METRICS */

export function MetricsPanel({
  run,
  metrics,
}: {
  run: RunDetail;
  metrics: Metrics;
}) {
  const hasCost = metrics.cost_usd !== null && metrics.cost_usd !== undefined;
  return (
    <div>
      <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border)] sm:grid-cols-4">
        <Stat label="Iterations" value={formatNumber(metrics.iterations)} />
        <Stat label="Tool calls" value={formatNumber(metrics.tool_calls)} />
        <Stat label="Model calls" value={formatNumber(metrics.model_calls)} />
        <Stat label="Runtime" value={formatDuration(metrics.runtime_s)} />
        <Stat label="Files inspected" value={formatNumber(metrics.files_inspected)} />
        <Stat
          label="Files modified"
          value={formatNumber(metrics.files_modified)}
          tone={metrics.files_modified ? "ok" : undefined}
        />
        <Stat label="Test runs" value={formatNumber(metrics.test_runs)} />
        <Stat label="Hypotheses" value={formatNumber(metrics.hypotheses)} />
        <Stat label="Input tokens" value={formatNumber(metrics.input_tokens)} />
        <Stat label="Output tokens" value={formatNumber(metrics.output_tokens)} />
        <Stat
          label="Cached tokens"
          value={formatNumber(metrics.cached_tokens)}
          hint="prompt cache reads"
        />
        <Stat
          label="Est. cost"
          value={hasCost ? formatCost(metrics.cost_usd) : "—"}
          hint={hasCost ? "published list prices" : "pricing unknown for this model"}
        />
      </div>

      {(metrics.loop_signals ?? 0) > 0 && (
        <div className="border-t border-[var(--border)] px-4 py-2.5">
          <span className="text-[12px] text-[var(--color-warn)]">
            {metrics.loop_signals} repetition signal
            {metrics.loop_signals === 1 ? "" : "s"} detected — recovery directives were
            injected.
          </span>
        </div>
      )}

      <div className="border-t border-[var(--border)]">
        <p className="px-4 pt-3 text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
          Configuration (persisted for reproducibility)
        </p>
        <pre className="terminal overflow-x-auto px-4 pb-3 pt-2 text-[var(--text-muted)]">
          {JSON.stringify(run.config ?? {}, null, 2)}
        </pre>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- HYPOTHESES */

export function HypothesesPanel({ hypotheses }: { hypotheses: Hypothesis[] }) {
  if (hypotheses.length === 0) {
    return (
      <EmptyState
        title="No hypothesis recorded"
        description="The agent records a structured hypothesis before editing code."
        icon="◇"
      />
    );
  }
  return (
    <div className="divide-y divide-[var(--border)]">
      {hypotheses.map((hypothesis) => (
        <div key={hypothesis.id} className="px-4 py-3">
          <div className="flex items-center gap-2">
            <Badge tone="accent">#{hypothesis.index}</Badge>
            <span className="mono text-[11px] text-[var(--text-dim)]">
              iteration {hypothesis.iteration}
            </span>
            {hypothesis.supersedes_previous && <Badge tone="warn">supersedes</Badge>}
          </div>
          <p className="mt-2 text-[13px] font-medium text-[var(--text)]">
            {hypothesis.suspected_cause}
          </p>
          <dl className="mt-2 space-y-1.5">
            {(
              [
                ["Evidence", hypothesis.evidence, false],
                ["Files", hypothesis.files_involved.join(", "), true],
                ["Proposed fix", hypothesis.proposed_fix, false],
                ["Validation", hypothesis.validation_plan, true],
              ] as [string, string, boolean][]
            ).map(([label, value, mono]) =>
              value ? (
                <div key={label} className="flex gap-3 text-[12px]">
                  <dt className="w-24 shrink-0 text-[var(--text-dim)]">{label}</dt>
                  <dd
                    className={`min-w-0 flex-1 text-[var(--text-muted)] ${mono ? "mono text-[11px]" : ""}`}
                  >
                    {value}
                  </dd>
                </div>
              ) : null,
            )}
          </dl>
        </div>
      ))}
    </div>
  );
}
