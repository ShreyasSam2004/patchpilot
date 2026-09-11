"use client";

/**
 * The live execution workspace.
 *
 * Events arrive over SSE with a resume cursor, so reconnecting never drops or
 * replays anything, and a finished run renders identically from storage. The
 * detail panels are refetched when the run reaches a terminal state, and while
 * it is live they are refreshed on the events that make them stale.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  DiffPanel,
  FilesPanel,
  HypothesesPanel,
  MetricsPanel,
  TestsPanel,
  TracePanel,
} from "@/components/panels";
import { ReviewPanel } from "@/components/review";
import { Timeline } from "@/components/timeline";
import {
  Badge,
  Button,
  ErrorBanner,
  Panel,
  RunStateBadge,
  Spinner,
  Tabs,
} from "@/components/ui";
import { ApiError, api, streamRunEvents } from "@/lib/api";
import { formatDuration, formatRelative, formatTimestamp } from "@/lib/format";
import {
  isTerminal,
  type Diff,
  type Hypothesis,
  type RunDetail,
  type RunEvent,
  type Review,
  type RunFiles,
  type TestRun,
  type ToolCall,
} from "@/lib/types";

type Tab = "overview" | "review" | "files" | "patch" | "tests" | "trace" | "metrics";

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const runId = params?.id ?? "";

  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [diff, setDiff] = useState<Diff | null>(null);
  const [tests, setTests] = useState<TestRun[]>([]);
  const [calls, setCalls] = useState<ToolCall[]>([]);
  const [files, setFiles] = useState<RunFiles | null>(null);
  const [hypotheses, setHypotheses] = useState<Hypothesis[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [review, setReview] = useState<Review | null>(null);
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Whether the event stream should be open. Deliberately *not* derived from
  // the run's state: see the streaming effect below.
  const [streaming, setStreaming] = useState(false);

  const lastSeq = useRef(0);
  const tabChosen = useRef(false);
  const live = run !== null && !isTerminal(run.state);

  const loadDetails = useCallback(async (): Promise<Review | null> => {
    if (!runId) return null;
    try {
      const [d, t, c, f, h, r] = await Promise.all([
        api.getDiff(runId),
        api.getTests(runId),
        api.getTools(runId),
        api.getFiles(runId),
        api.getHypotheses(runId),
        api.getReview(runId).catch(() => null),
      ]);
      setDiff(d);
      setTests(t);
      setCalls(c);
      setFiles(f);
      setHypotheses(h);
      setReview(r);
      // A run waiting on a person should open on the decision, not bury it
      // behind a tab they have to notice. Only ever done automatically once,
      // and never over a tab the user chose themselves.
      if (r?.review_state === "PATCH_READY" && !tabChosen.current) {
        setTab("review");
      }
      return r;
    } catch {
      /* panels are secondary; the run header and timeline still render */
      return null;
    }
  }, [runId]);

  const loadRun = useCallback(async () => {
    if (!runId) return null;
    try {
      const detail = await api.getRun(runId);
      setRun(detail);
      setError(null);
      return detail;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      return null;
    }
  }, [runId]);

  /** Pick up the review once the run has finished producing it.
   *
   * The run reaches a terminal state *before* its patch is recorded for review:
   * the orchestrator sets RESOLVED and emits the event, and only then is the
   * result persisted and `record_generated_patch` called. So one fetch on the
   * state change is a race, and losing it is what left the review invisible
   * until a manual reload.
   *
   * A run with nothing worth reviewing keeps `review_state` at RUNNING forever,
   * which is why that alone cannot be the stop condition -- hence a bounded
   * retry rather than a wait.
   */
  const settleAfterFinish = useCallback(async () => {
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const detail = await loadRun();
      const found = await loadDetails();
      if (found || !detail || detail.review_state !== "RUNNING") return;
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  }, [loadRun, loadDetails]);

  // Initial load: run, full event history, and all panels.
  useEffect(() => {
    (async () => {
      const detail = await loadRun();
      if (detail) {
        try {
          const history = await api.getEvents(runId);
          setEvents(history);
          lastSeq.current = history.at(-1)?.seq ?? 0;
        } catch {
          /* an empty timeline is better than a broken page */
        }
        await loadDetails();
        // Opened while the run is still going: follow it to completion.
        if (!isTerminal(detail.state)) setStreaming(true);
      }
      setLoading(false);
    })();
  }, [runId, loadRun, loadDetails]);

  // Live stream while the run is active.
  //
  // The dependency list must not contain `run`. It used to, and every
  // STATE_CHANGED event calls loadRun(), which stores a fresh object — so the
  // effect tore the EventSource down and reopened it on each of the ~15 state
  // transitions in a run. The churn (plus the browser's per-origin connection
  // limit) left the stream dead, and the timeline only appeared at the end when
  // onDone refetched everything.
  //
  // Nor may it contain `live`. That was the next version of the same mistake:
  // `live` goes false the moment loadRun() sees a terminal state, so React ran
  // the cleanup and closed the EventSource *before* the server's `done` frame
  // arrived — and `done` is what triggers the final refresh that fetches the
  // review. The run finished, the review existed, and the page never asked for
  // it until someone reloaded by hand.
  //
  // `streaming` is latched instead: set once when a live run is opened, cleared
  // only by `done`. The server sends that only after the run is terminal and no
  // events remain, so the stream now closes when the conversation is actually
  // over rather than when a derived flag flips.
  useEffect(() => {
    if (!runId || !streaming) return;

    const stop = streamRunEvents(runId, lastSeq.current, {
      onEvent: (event) => {
        lastSeq.current = Math.max(lastSeq.current, event.seq);
        setEvents((previous) =>
          previous.some((e) => e.seq === event.seq) ? previous : [...previous, event],
        );

        // Refresh only the panels this event actually invalidates.
        if (event.type === "STATE_CHANGED") void loadRun();
        if (event.type === "PATCH_APPLIED") {
          void api.getDiff(runId).then(setDiff).catch(() => {});
          void api.getFiles(runId).then(setFiles).catch(() => {});
        }
        if (event.type === "TEST_FINISHED") {
          void api.getTests(runId).then(setTests).catch(() => {});
        }
        if (event.type === "HYPOTHESIS_CREATED") {
          void api.getHypotheses(runId).then(setHypotheses).catch(() => {});
        }
        if (event.type === "TOOL_RESULT") {
          void api.getTools(runId).then(setCalls).catch(() => {});
        }
      },
      onDone: () => {
        setStreaming(false);
        void settleAfterFinish();
      },
    });
    return stop;
    // Every callback here is a useCallback keyed on runId, so none of them
    // change while a run is in flight; only `streaming` can retrigger this.
  }, [runId, streaming, loadRun, settleAfterFinish]);

  const decide = useCallback(
    async (kind: string, action: () => Promise<Review>) => {
      setReviewBusy(kind);
      setReviewError(null);
      try {
        setReview(await action());
        await loadRun();
      } catch (err) {
        setReviewError(err instanceof ApiError ? err.message : String(err));
        // Re-read: a refused decision usually means our view was stale.
        await api.getReview(runId).then(setReview).catch(() => {});
      } finally {
        setReviewBusy(null);
      }
    },
    [runId, loadRun],
  );

  const onApprove = useCallback(
    () =>
      decide("approve", () =>
        // The digest we rendered. If the patch moved underneath us the server
        // refuses rather than approving something the reviewer never saw.
        api.approveReview(runId, { expected_digest: review?.current?.digest ?? null }),
      ),
    [decide, runId, review],
  );

  const onRequestRevision = useCallback(
    (feedback: string) => decide("revision", () => api.requestRevision(runId, { feedback })),
    [decide, runId],
  );

  const onReject = useCallback(
    (feedback: string) => decide("reject", () => api.rejectReview(runId, { feedback })),
    [decide, runId],
  );

  const onPublish = useCallback(async () => {
    setReviewBusy("publish");
    setReviewError(null);
    try {
      await api.publishRun(runId, {});
      setReview(await api.getReview(runId));
      await loadRun();
    } catch (err) {
      setReviewError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setReviewBusy(null);
    }
  }, [runId, loadRun]);

  const onSelectVersion = useCallback(
    async (version: number) => {
      try {
        const picked = await api.getPatchVersion(runId, version);
        setDiff(null);
        setReview((previous) => (previous ? { ...previous, current: picked } : previous));
        setTab("review");
      } catch {
        /* the current version stays on screen */
      }
    },
    [runId],
  );

  if (loading) return <Spinner label="Loading run…" />;

  if (error && !run) {
    return (
      <div className="space-y-4">
        <ErrorBanner message={error} onRetry={() => void loadRun()} />
        <Link href="/runs" className="text-[13px] text-[var(--color-accent)] hover:underline">
          ← Back to runs
        </Link>
      </div>
    );
  }

  if (!run) return null;

  const metrics = run.metrics ?? {};
  const lastTest = tests.at(-1);

  return (
    <div className="space-y-4">
      {/* ---------------------------------------------------------- header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <Link
              href="/runs"
              className="text-[12px] text-[var(--text-dim)] hover:text-[var(--text)]"
            >
              Runs
            </Link>
            <span className="text-[var(--text-dim)]">/</span>
            <span className="mono text-[13px] font-semibold text-[var(--text)]">
              Run #{run.code}
            </span>
            <RunStateBadge state={run.state} />
            {/* The review state is a separate claim from the agent's own, and
                belongs beside it: "the agent finished" is not "a human approved
                this". */}
            {review && review.review_state !== "RUNNING" && (
              <button type="button" onClick={() => setTab("review")}>
                <Badge
                  tone={
                    review.review_state === "PR_CREATED" ||
                    review.review_state === "APPROVED"
                      ? "ok"
                      : review.review_state === "REJECTED" ||
                          review.review_state === "PUBLISH_FAILED"
                        ? "bad"
                        : "warn"
                  }
                >
                  {review.review_state === "PATCH_READY"
                    ? "awaiting review"
                    : review.review_state.replace(/_/g, " ").toLowerCase()}
                </Badge>
              </button>
            )}
            {run.is_benchmark_task && <Badge tone="accent">benchmark task</Badge>}
          </div>
          <h1 className="mt-1.5 truncate text-lg font-semibold tracking-tight">
            {run.issue_title || run.repository}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-[var(--text-dim)]">
            <span className="mono">{run.repository}</span>
            {run.base_commit && (
              <span className="mono" title={run.base_commit}>
                @{run.base_commit.slice(0, 8)}
              </span>
            )}
            <span>
              {run.provider} / <span className="mono">{run.model}</span>
            </span>
            <span>{run.sandbox} sandbox</span>
            <span>queued {formatRelative(run.queued_at)}</span>
            {run.issue_url && (
              <a
                href={run.issue_url}
                target="_blank"
                rel="noreferrer noopener"
                className="text-[var(--color-accent)] hover:underline"
              >
                issue ↗
              </a>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {live && !run.cancel_requested && (
            <Button
              variant="danger"
              size="sm"
              onClick={async () => {
                await api.cancelRun(run.id).catch(() => {});
                void loadRun();
              }}
            >
              Cancel run
            </Button>
          )}
          {run.cancel_requested && live && <Badge tone="warn">cancelling…</Badge>}
        </div>
      </div>

      {run.error && <ErrorBanner message={run.error} />}

      {/* ------------------------------------------------------ status strip */}
      <div className="surface grid grid-cols-2 divide-x divide-y divide-[var(--border)] sm:grid-cols-3 lg:grid-cols-6 lg:divide-y-0">
        <HeaderStat label="Status" value={<RunStateBadge state={run.state} />} />
        <HeaderStat label="Iterations" value={metrics.iterations ?? 0} />
        <HeaderStat label="Tool calls" value={metrics.tool_calls ?? 0} />
        <HeaderStat label="Files changed" value={metrics.files_modified ?? 0} />
        <HeaderStat
          label="Latest tests"
          value={
            lastTest ? (
              <span className={lastTest.green ? "text-[var(--color-ok)]" : "text-[var(--color-bad)]"}>
                {lastTest.passed}/{lastTest.passed + lastTest.failed + lastTest.errors}
              </span>
            ) : (
              "—"
            )
          }
        />
        <HeaderStat label="Runtime" value={formatDuration(metrics.runtime_s)} />
      </div>

      {/* ------------------------------------------------------------ body */}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
        <div className="min-w-0 space-y-4">
          <Panel
            title="Detail"
            actions={
              <span className="mono text-[11px] text-[var(--text-dim)]">
                {events.length} events
              </span>
            }
          >
            <Tabs<Tab>
              active={tab}
              onChange={(next) => {
                tabChosen.current = true;
                setTab(next);
              }}
              tabs={[
                { id: "overview", label: "Overview" },
                ...(review?.current
                  ? [
                      {
                        id: "review" as const,
                        label: "Review",
                        count: review.versions.length,
                      },
                    ]
                  : []),
                { id: "files", label: "Files", count: files?.inspected_count },
                { id: "patch", label: "Patch", count: diff?.files.length },
                { id: "tests", label: "Tests", count: tests.length },
                { id: "trace", label: "Agent trace", count: calls.length },
                { id: "metrics", label: "Metrics" },
              ]}
            />

            {tab === "overview" && (
              <OverviewTab run={run} hypotheses={hypotheses} />
            )}
            {tab === "review" && review && (
              <div className="p-4">
                <ReviewPanel
                  review={review}
                  busy={reviewBusy}
                  error={reviewError}
                  onApprove={onApprove}
                  onRequestRevision={onRequestRevision}
                  onReject={onReject}
                  onPublish={onPublish}
                  onSelectVersion={onSelectVersion}
                />
              </div>
            )}
            {tab === "files" && <FilesPanel files={files} />}
            {tab === "patch" && <DiffPanel diff={diff} />}
            {tab === "tests" && <TestsPanel tests={tests} />}
            {tab === "trace" && <TracePanel calls={calls} />}
            {tab === "metrics" && <MetricsPanel run={run} metrics={metrics} />}
          </Panel>
        </div>

        <div className="min-w-0">
          <Panel
            title="Agent timeline"
            actions={
              live ? (
                <Badge tone="info" dot pulse>
                  live
                </Badge>
              ) : (
                <Badge tone="muted">finished</Badge>
              )
            }
          >
            <Timeline events={events} live={live} />
          </Panel>
        </div>
      </div>
    </div>
  );
}

function HeaderStat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="px-4 py-2.5">
      <div className="text-[10px] uppercase tracking-wider text-[var(--text-dim)]">
        {label}
      </div>
      <div className="mono mt-1 text-[15px] font-semibold tabular-nums text-[var(--text)]">
        {value}
      </div>
    </div>
  );
}

function OverviewTab({
  run,
  hypotheses,
}: {
  run: RunDetail;
  hypotheses: Hypothesis[];
}) {
  return (
    <div className="divide-y divide-[var(--border)]">
      {run.summary && (
        <div className="px-4 py-3">
          <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
            Outcome
          </p>
          <p className="mt-1 text-[13px] text-[var(--text)]">{run.summary}</p>
          {run.stop_reason && run.stop_reason !== run.summary && (
            <p className="mt-1 text-[12px] text-[var(--text-dim)]">
              Stopped because: {run.stop_reason}
            </p>
          )}
          {run.failure_class && (
            <p className="mt-1.5">
              <Badge tone="warn">failure: {run.failure_class.toLowerCase()}</Badge>
            </p>
          )}
        </div>
      )}

      <div className="px-4 py-3">
        <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">Issue</p>
        <pre className="terminal mt-1.5 max-h-72 overflow-auto text-[var(--text-muted)]">
          {run.issue}
        </pre>
      </div>

      <div>
        <p className="px-4 pt-3 text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
          Hypotheses
        </p>
        <HypothesesPanel hypotheses={hypotheses} />
      </div>

      <div className="px-4 py-3">
        <p className="text-[11px] uppercase tracking-wider text-[var(--text-dim)]">
          Reproduction
        </p>
        <dl className="mt-1.5 space-y-1 text-[12px]">
          {(
            [
              ["Repository", run.repo_url || run.repository],
              ["Base commit", run.base_commit || "default branch"],
              ["Test command", run.test_command || "auto-discovered"],
              ["Provider / model", `${run.provider} / ${run.model}`],
              ["Sandbox", run.sandbox],
              ["Queued", formatTimestamp(run.queued_at)],
              ["Started", formatTimestamp(run.started_at)],
              ["Finished", formatTimestamp(run.finished_at)],
            ] as [string, string][]
          ).map(([key, value]) => (
            <div key={key} className="flex gap-3">
              <dt className="w-32 shrink-0 text-[var(--text-dim)]">{key}</dt>
              <dd className="mono min-w-0 flex-1 break-all text-[var(--text-muted)]">
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}
