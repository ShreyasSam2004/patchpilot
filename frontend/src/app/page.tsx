"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge, Panel, Spinner, Stat, WarningBanner } from "@/components/ui";
import { api } from "@/lib/api";
import { formatPercent, formatRelative } from "@/lib/format";
import type { Leaderboard, RunStats, RunSummary, SystemStatus } from "@/lib/types";
import { RunStateBadge } from "@/components/ui";

export default function HomePage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [leaderboard, setLeaderboard] = useState<Leaderboard | null>(null);
  const [stats, setStats] = useState<RunStats | null>(null);
  const [recent, setRecent] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const [s, l, st, runs] = await Promise.all([
          api.status(),
          api.leaderboard(),
          api.stats(),
          api.listRuns({ limit: 6 }),
        ]);
        setStatus(s);
        setLeaderboard(l);
        setStats(st);
        setRecent(runs.items);
      } catch {
        setOffline(true);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return (
    <div className="space-y-6">
      <section className="grid gap-6 lg:grid-cols-[1.35fr_1fr]">
        <div className="flex flex-col justify-center gap-4 py-4">
          <div className="flex items-center gap-2">
            <Badge tone="accent">autonomous agent</Badge>
            <Badge tone="muted">SWE-bench Lite</Badge>
          </div>
          <h1 className="text-3xl font-semibold leading-tight tracking-tight text-[var(--text)]">
            Give it an issue and a repository.
            <br />
            <span className="text-[var(--text-muted)]">
              It reads the code, patches it, and runs your tests until they pass.
            </span>
          </h1>
          <p className="max-w-2xl text-[13px] leading-relaxed text-[var(--text-muted)]">
            PatchPilot is a bounded autonomous loop, not a chat window. It localises the
            bug, records a structured hypothesis, edits code with patch-based tools, runs
            the test suite in an isolated sandbox, reads the failures, and iterates. Every
            tool call, patch attempt and test result is recorded and replayable.
          </p>
          <div className="flex flex-wrap items-center gap-3 pt-1">
            <Link
              href="/runs/new"
              className="focus-ring rounded-md bg-[var(--color-accent)] px-4 py-2 text-[13px] font-semibold text-white transition-colors hover:bg-[var(--color-accent)]/85"
            >
              Start autonomous fix
            </Link>
            <Link
              href="/results"
              className="focus-ring rounded-md border border-[var(--border-strong)] bg-[var(--bg-input)] px-4 py-2 text-[13px] font-medium text-[var(--text)] transition-colors hover:bg-[var(--border)]"
            >
              SWE-bench results
            </Link>
          </div>
        </div>

        <LoopDiagram />
      </section>

      {offline && (
        <WarningBanner
          items={[
            "The PatchPilot API is not reachable. Start it with: cd backend && python -m patchpilot.cli serve",
          ]}
        />
      )}
      {status && status.warnings.length > 0 && <WarningBanner items={status.warnings} />}

      <section className="grid gap-4 md:grid-cols-2">
        <Panel title="SWE-bench Lite result">
          {loading ? (
            <Spinner />
          ) : leaderboard?.has_results && leaderboard.best ? (
            <div>
              <div className="grid grid-cols-3 divide-x divide-[var(--border)]">
                <Stat
                  label="Resolve rate"
                  value={formatPercent(leaderboard.best.resolve_rate)}
                  tone="ok"
                  hint="official evaluator"
                />
                <Stat
                  label="Resolved"
                  value={leaderboard.best.resolved_tasks}
                  hint={`of ${
                    leaderboard.best.resolved_tasks + leaderboard.best.unresolved_tasks
                  } graded`}
                />
                <Stat label="Model" value={<span className="text-sm">{leaderboard.best.model}</span>} />
              </div>
              <div className="border-t border-[var(--border)] px-4 py-3">
                <Link
                  href="/results"
                  className="text-[12px] text-[var(--color-accent)] hover:underline"
                >
                  See all results →
                </Link>
              </div>
            </div>
          ) : (
            <div className="px-4 py-6">
              <p className="text-[13px] font-medium text-[var(--text)]">
                No benchmark results yet
              </p>
              <p className="mt-1.5 max-w-lg text-[12px] leading-relaxed text-[var(--text-dim)]">
                This space stays empty until the official SWE-bench harness has actually
                graded a batch. PatchPilot never displays an estimated or placeholder
                resolve rate — that is the whole point of benchmarking it.
              </p>
              <code className="mono mt-3 block w-fit rounded border border-[var(--border)] bg-[var(--bg-input)] px-2.5 py-1.5 text-[11px] text-[var(--text-muted)]">
                patchpilot bench run --limit 10
              </code>
            </div>
          )}
        </Panel>

        <Panel title="Local activity">
          {loading ? (
            <Spinner />
          ) : (
            <>
              <div className="grid grid-cols-3 divide-x divide-[var(--border)]">
                <Stat label="Runs" value={stats?.total ?? 0} />
                <Stat label="Active" value={stats?.active ?? 0} tone="warn" />
                <Stat
                  label="Agent resolved"
                  value={
                    stats?.agent_success_rate === null || stats?.agent_success_rate === undefined
                      ? "—"
                      : formatPercent(stats.agent_success_rate)
                  }
                  hint="agent's own verdict"
                />
              </div>
              <div className="divide-y divide-[var(--border)] border-t border-[var(--border)]">
                {recent.length === 0 ? (
                  <p className="px-4 py-6 text-[12px] text-[var(--text-dim)]">
                    No runs yet.
                  </p>
                ) : (
                  recent.map((run) => (
                    <Link
                      key={run.id}
                      href={`/runs/${run.id}`}
                      className="flex items-center gap-3 px-4 py-2 transition-colors hover:bg-[var(--bg-input)]"
                    >
                      <span className="mono text-[11px] text-[var(--text-dim)]">
                        #{run.code}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[12px] text-[var(--text-muted)]">
                        {run.issue_title || run.repository}
                      </span>
                      <RunStateBadge state={run.state} />
                      <span className="hidden text-[11px] text-[var(--text-dim)] sm:inline">
                        {formatRelative(run.queued_at)}
                      </span>
                    </Link>
                  ))
                )}
              </div>
            </>
          )}
        </Panel>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <FeatureCard
          title="Iterative test feedback"
          body="A patch is a hypothesis, not an answer. Every edit is followed by a real test run in a sandbox, and every failure is classified before the next attempt. finish(resolved=true) is refused unless the tests actually went green."
        />
        <FeatureCard
          title="Tools, native or MCP"
          body="One typed tool interface with validation, timeouts and output caps. Native tools and MCP-discovered tools sit in the same registry behind a default-deny policy: the server decides what exists, PatchPilot decides what is allowed, the model decides what to call."
        />
        <FeatureCard
          title="Graded, not self-reported"
          body="The agent's own verdict and the official SWE-bench evaluator's verdict are stored in separate columns and shown separately. Only the evaluator's verdict feeds a reported resolve rate."
        />
      </section>
    </div>
  );
}

function FeatureCard({ title, body }: { title: string; body: string }) {
  return (
    <div className="surface p-4">
      <h3 className="text-[13px] font-semibold text-[var(--text)]">{title}</h3>
      <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--text-dim)]">{body}</p>
    </div>
  );
}

/** The loop, drawn as the terminal output it actually produces. */
function LoopDiagram() {
  const lines: [string, string][] = [
    ["dim", "$ patchpilot run --fixture calculator_bug"],
    ["", ""],
    ["ok", "✓ repository prepared        local sandbox"],
    ["ok", "✓ search_code                'divide' — 4 matches"],
    ["ok", "✓ read_file                  src/calculator.py"],
    ["accent", "● hypothesis                 divide() has no zero guard"],
    ["ok", "✓ edit_file                  src/calculator.py"],
    ["ok", "✓ run_tests                  pytest tests/ -q"],
    ["bad", "    7 passed, 3 failed"],
    ["warn", "● analysing failures…        REGRESSION"],
    ["ok", "✓ edit_file                  src/calculator.py"],
    ["ok", "✓ run_tests                  pytest tests/ -q"],
    ["ok", "    10 passed, 0 failed"],
    ["", ""],
    ["ok", "RESOLVED — 6 iterations, 1 patch, 2 test runs"],
  ];
  const tone: Record<string, string> = {
    ok: "text-[var(--color-ok)]",
    bad: "text-[var(--color-bad)]",
    warn: "text-[var(--color-warn)]",
    accent: "text-[var(--color-accent)]",
    dim: "text-[var(--text-dim)]",
    "": "",
  };
  return (
    <div className="surface overflow-hidden">
      <div className="flex items-center gap-1.5 border-b border-[var(--border)] bg-[var(--bg-raised)] px-3 py-2">
        <span className="h-2.5 w-2.5 rounded-full bg-[var(--color-bad)]/70" />
        <span className="h-2.5 w-2.5 rounded-full bg-[var(--color-warn)]/70" />
        <span className="h-2.5 w-2.5 rounded-full bg-[var(--color-ok)]/70" />
        <span className="mono ml-2 text-[11px] text-[var(--text-dim)]">agent timeline</span>
      </div>
      <div className="terminal px-4 py-3">
        {lines.map((line, index) => (
          <div key={index} className={tone[line[0]] ?? "text-[var(--text-muted)]"}>
            {line[1] || " "}
          </div>
        ))}
      </div>
      <p className="border-t border-[var(--border)] px-4 py-2 text-[11px] text-[var(--text-dim)]">
        Illustrative layout of the timeline view. Live runs render from persisted events.
      </p>
    </div>
  );
}
