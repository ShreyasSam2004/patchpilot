"use client";

/**
 * The agent timeline.
 *
 * Renders exclusively from persisted events, so a live run and a replayed
 * finished run are identical. Only structured actions are shown — tool calls,
 * their outcomes, recorded hypotheses, test results and lifecycle transitions.
 * No model reasoning is stored or displayed.
 */

import { useEffect, useRef } from "react";

import { formatMs, formatTime } from "@/lib/format";
import type { RunEvent } from "@/lib/types";

interface Row {
  key: string;
  mark: string;
  tone: string;
  label: string;
  detail?: string;
  time: string;
  iteration: number;
  body?: React.ReactNode;
}

const TONE: Record<string, string> = {
  ok: "text-[var(--color-ok)]",
  bad: "text-[var(--color-bad)]",
  warn: "text-[var(--color-warn)]",
  info: "text-[var(--color-info)]",
  accent: "text-[var(--color-accent)]",
  dim: "text-[var(--text-dim)]",
};

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function toRow(event: RunEvent): Row | null {
  const p = event.payload;
  const time = formatTime(event.created_at);
  const base = { key: String(event.seq), time, iteration: event.iteration };

  switch (event.type) {
    case "REPOSITORY_READY":
      return {
        ...base,
        mark: "✓",
        tone: "ok",
        label: "Repository prepared",
        detail: `${str(p.repository)} · ${str(p.sandbox)} sandbox (${str(p.isolation)} isolation)`,
      };
    case "ISSUE_ANALYZED": {
      const tools = Array.isArray(p.tools_available) ? p.tools_available.length : 0;
      return {
        ...base,
        mark: "✓",
        tone: "ok",
        label: "Issue analysed",
        detail: `${tools} tools available · test command: ${str(p.test_command, "auto")}`,
      };
    }
    case "AGENT_MESSAGE":
      return {
        ...base,
        mark: "›",
        tone: "dim",
        label: str(p.text),
      };
    case "TOOL_RESULT": {
      const ok = p.ok !== false;
      return {
        ...base,
        mark: ok ? "✓" : "✗",
        tone: ok ? "ok" : "bad",
        label: str(p.tool),
        detail: `${str(p.summary)} · ${formatMs(num(p.duration_ms))}`,
      };
    }
    case "HYPOTHESIS_CREATED":
      return {
        ...base,
        mark: "●",
        tone: "accent",
        label: `Hypothesis #${num(p.index, 1)}${p.supersedes ? " (supersedes previous)" : ""}`,
        detail: str(p.cause),
        body: (
          <div className="mt-1.5 space-y-1 rounded border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
            <HypothesisRow label="Evidence" value={str(p.evidence)} />
            <HypothesisRow
              label="Files"
              value={Array.isArray(p.files) ? p.files.join(", ") : ""}
              mono
            />
            <HypothesisRow label="Proposed fix" value={str(p.fix)} />
            <HypothesisRow label="Validation" value={str(p.validation)} mono />
          </div>
        ),
      };
    case "PATCH_APPLIED": {
      const files = Array.isArray(p.files) ? p.files : [];
      return {
        ...base,
        mark: "✓",
        tone: "ok",
        label: "Patch applied",
        detail: `${files.join(", ") || "workspace"} · +${num(p.insertions)}/-${num(p.deletions)}`,
      };
    }
    case "TEST_STARTED":
      return {
        ...base,
        mark: "▸",
        tone: "info",
        label: "Running tests",
        detail: str(p.command),
      };
    case "TEST_FINISHED": {
      const green = p.green === true;
      const parts = [`${num(p.passed)} passed`];
      if (num(p.failed)) parts.push(`${num(p.failed)} failed`);
      if (num(p.errors)) parts.push(`${num(p.errors)} errors`);
      if (num(p.skipped)) parts.push(`${num(p.skipped)} skipped`);
      const failing = Array.isArray(p.failing_tests) ? p.failing_tests : [];
      return {
        ...base,
        mark: green ? "✓" : "✗",
        tone: green ? "ok" : "bad",
        label: parts.join(", "),
        detail: `${formatMs(num(p.duration_ms))} · ${str(p.framework, "unknown")}`,
        body:
          failing.length > 0 ? (
            <ul className="mono mt-1.5 space-y-0.5 rounded border border-[var(--color-bad)]/25 bg-[var(--color-bad-soft)]/50 px-3 py-2 text-[11px] text-[var(--color-bad)]">
              {failing.slice(0, 8).map((test) => (
                <li key={String(test)}>{String(test)}</li>
              ))}
              {failing.length > 8 && (
                <li className="text-[var(--text-dim)]">+{failing.length - 8} more</li>
              )}
            </ul>
          ) : undefined,
      };
    }
    case "LOOP_DETECTED":
      return {
        ...base,
        mark: "⟳",
        tone: "warn",
        label: "Repetition detected",
        detail: str(p.detail),
      };
    case "RECOVERY_TRIGGERED":
      return {
        ...base,
        mark: "↻",
        tone: "warn",
        label: "Recovery directive injected",
        detail: str(p.directive).slice(0, 200),
      };
    case "BUDGET_EXCEEDED":
      return {
        ...base,
        mark: "⏱",
        tone: "warn",
        label: "Budget exhausted",
        detail: str(p.limit),
      };
    case "WARNING":
      return { ...base, mark: "!", tone: "warn", label: str(p.message) };
    case "RUN_RESOLVED":
      return {
        ...base,
        mark: "✓",
        tone: "ok",
        label: "RESOLVED",
        detail: str(p.summary) || str(p.stop_reason),
      };
    case "RUN_FAILED":
      return {
        ...base,
        mark: "✗",
        tone: "bad",
        label: "NOT RESOLVED",
        detail: str(p.error) || str(p.stop_reason),
      };
    case "RUN_CANCELLED":
      return { ...base, mark: "■", tone: "dim", label: "CANCELLED", detail: str(p.stop_reason) };
    default:
      return null; // lifecycle noise the timeline does not need
  }
}

function HypothesisRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  if (!value) return null;
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="w-20 shrink-0 text-[var(--text-dim)]">{label}</span>
      <span className={`min-w-0 flex-1 text-[var(--text-muted)] ${mono ? "mono" : ""}`}>
        {value}
      </span>
    </div>
  );
}

export function Timeline({
  events,
  live,
  autoScroll = true,
}: {
  events: RunEvent[];
  live: boolean;
  autoScroll?: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const rows = events.map(toRow).filter((row): row is Row => row !== null);

  useEffect(() => {
    if (live && autoScroll) {
      endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [rows.length, live, autoScroll]);

  if (rows.length === 0) {
    return (
      <p className="px-4 py-8 text-center text-[12px] text-[var(--text-dim)]">
        {live ? "Waiting for the agent to start…" : "No timeline events recorded."}
      </p>
    );
  }

  let lastIteration = -1;

  return (
    <div className="max-h-[640px] overflow-y-auto px-4 py-3">
      {rows.map((row) => {
        const newIteration = row.iteration > 0 && row.iteration !== lastIteration;
        if (newIteration) lastIteration = row.iteration;
        return (
          <div key={row.key}>
            {newIteration && (
              <div className="mt-3 mb-1.5 flex items-center gap-2 first:mt-0">
                <span className="mono text-[10px] uppercase tracking-widest text-[var(--text-dim)]">
                  iteration {row.iteration}
                </span>
                <span className="h-px flex-1 bg-[var(--border)]" />
              </div>
            )}
            <div className="fade-in flex items-baseline gap-2.5 py-0.5">
              <span className={`mono w-3 shrink-0 text-[12px] ${TONE[row.tone]}`}>
                {row.mark}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className={`mono text-[12px] font-medium ${TONE[row.tone]}`}>
                    {row.label}
                  </span>
                  {row.detail && (
                    <span className="min-w-0 text-[11px] text-[var(--text-dim)]">
                      {row.detail}
                    </span>
                  )}
                </div>
                {row.body}
              </div>
              <span className="mono hidden shrink-0 text-[10px] text-[var(--text-dim)] sm:block">
                {row.time}
              </span>
            </div>
          </div>
        );
      })}
      {live && (
        <div className="flex items-center gap-2.5 py-1">
          <span className="mono w-3 text-[12px] text-[var(--color-accent)] pulse">●</span>
          <span className="text-[11px] text-[var(--text-dim)]">working…</span>
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}
