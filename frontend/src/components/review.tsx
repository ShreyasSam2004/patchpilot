"use client";

/**
 * The human review surface: what the agent proposes, and what a person decides.
 *
 * Two deliberate choices in the wording. The approve button says "Approve &
 * Create PR", not "Approve", because clicking it publishes a branch — a button
 * whose label understates what it does is a trap. And nowhere does this
 * component offer to merge, because PatchPilot cannot: the pull request goes
 * into the team's normal process.
 */

import { useState } from "react";

import type {
  Publication,
  Review,
  PatchVersion,
  ReviewState,
} from "@/lib/types";
import { Badge, Button, Panel, Textarea } from "@/components/ui";

const STATE_LABEL: Record<ReviewState, string> = {
  RUNNING: "Agent working",
  PATCH_READY: "Awaiting approval",
  REVISION_REQUESTED: "Revision requested",
  REVISING: "Agent revising",
  APPROVED: "Approved",
  PUBLISHING: "Publishing",
  PR_CREATED: "Pull request open",
  REJECTED: "Rejected",
  PUBLISH_FAILED: "Publication failed",
};

const STATE_TONE: Record<ReviewState, "ok" | "warn" | "bad" | "muted"> = {
  RUNNING: "muted",
  PATCH_READY: "warn",
  REVISION_REQUESTED: "warn",
  REVISING: "muted",
  APPROVED: "ok",
  PUBLISHING: "muted",
  PR_CREATED: "ok",
  REJECTED: "bad",
  PUBLISH_FAILED: "bad",
};

/** Human-readable publication failures. Never shows a raw provider body. */
const ERROR_LABEL: Record<string, string> = {
  NOT_CONFIGURED: "GitHub integration is not configured",
  AUTH_FAILED: "GitHub authentication failed",
  PERMISSION_DENIED: "Repository permission denied",
  REPOSITORY_UNKNOWN: "Repository not found or not recorded for this run",
  BASE_COMMIT_UNKNOWN: "This run has no base commit recorded",
  BASE_BRANCH_CHANGED: "The base branch changed",
  BRANCH_EXISTS: "A branch with this name already exists",
  PATCH_DIGEST_MISMATCH: "The patch changed after approval",
  PATCH_APPLY_FAILED: "The approved patch could not be applied",
  APPLIED_DIFF_MISMATCH: "The applied diff did not match the approved patch",
  PUSH_FAILED: "Push failed",
  PR_CREATE_FAILED: "Pull request creation failed",
  RATE_LIMITED: "GitHub rate limit reached",
  PROTECTED_BRANCH: "Refused: protected branch",
  UNKNOWN: "Publication failed",
};

function Verification({ version }: { version: PatchVersion }) {
  const v = version.verification ?? {};
  const recognised = v.summary_recognized === true;
  return (
    <div className="flex flex-wrap items-center gap-3 text-[12px]">
      <Badge tone={v.green ? "ok" : "bad"}>
        {v.passed ?? 0} passed · {v.failed ?? 0} failed · {v.errors ?? 0} errors
      </Badge>
      <span className="text-[var(--text-muted)]">
        {v.framework && v.framework !== "unknown" ? v.framework : "runner unknown"}
      </span>
      {/* A green run we could not parse is much weaker evidence than one we
          could, and the reviewer is the person who needs to know that. */}
      <Badge tone={recognised ? "ok" : "warn"}>
        {recognised ? "test output recognised" : "test output not recognised"}
      </Badge>
    </div>
  );
}

function Steps({ publication }: { publication: Publication }) {
  const done = new Set(publication.completed_steps ?? []);
  const rows: [string, string][] = [
    ["VERIFIED_DIGEST", "Approved patch verified"],
    ["RESOLVED_REPOSITORY", "Repository resolved"],
    ["CHECKED_DRIFT", "Base branch checked"],
    ["VERIFIED_APPLIED_DIFF", "Applied diff matches approval"],
    ["COMMIT_CREATED", "Commit created"],
    ["BRANCH_PUSHED", "Branch pushed"],
    ["PR_CREATED", "Pull request created"],
  ];
  return (
    <ul className="space-y-1 text-[12px]">
      {rows.map(([key, label]) => (
        <li key={key} className="flex items-center gap-2">
          <span className={done.has(key) ? "text-[var(--color-ok)]" : "text-[var(--text-muted)]"}>
            {done.has(key) ? "✓" : "○"}
          </span>
          <span className={done.has(key) ? "" : "text-[var(--text-muted)]"}>{label}</span>
        </li>
      ))}
    </ul>
  );
}

function Checks({ publication }: { publication: Publication }) {
  const runs = publication.checks?.runs ?? [];
  if (runs.length === 0) {
    return <p className="text-[12px] text-[var(--text-muted)]">No checks reported yet.</p>;
  }
  const glyph = (status: string, conclusion: string | null) => {
    if (status !== "completed") return "⏳";
    return conclusion === "success" ? "✓" : conclusion === "neutral" ? "–" : "✗";
  };
  return (
    <ul className="space-y-1 text-[12px]">
      {runs.map((c) => (
        <li key={c.name} className="flex items-center gap-2">
          <span>{glyph(c.status, c.conclusion)}</span>
          <span>{c.name}</span>
          <span className="text-[var(--text-muted)]">
            {c.status === "completed" ? (c.conclusion ?? "") : c.status.replace("_", " ")}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function PublicationPanel({ publication }: { publication: Publication }) {
  return (
    <Panel title="GitHub publication">
      <div className="space-y-4 px-4 py-3">
        <Steps publication={publication} />

        {publication.base_drifted && (
          <p className="rounded border border-[var(--color-warn)] p-2 text-[12px]">
            <strong>{publication.base_branch}</strong> moved while the agent was working.
            The branch was cut from the commit the patch was reviewed against, so the
            approved content is unchanged — resolve any conflicts in the pull request
            as usual.
          </p>
        )}

        {publication.error_code && (
          <p className="rounded border border-[var(--color-bad)] p-2 text-[12px]">
            <strong>{ERROR_LABEL[publication.error_code] ?? publication.error_code}</strong>
            {publication.error_detail ? <> — {publication.error_detail}</> : null}
          </p>
        )}

        {publication.pr_number && publication.pr_url && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-[13px]">
              <strong>Pull request #{publication.pr_number}</strong>
              <Badge tone={publication.pr_state === "open" ? "ok" : "muted"}>
                {(publication.pr_state ?? "open").toUpperCase()}
              </Badge>
            </div>
            <p className="font-mono text-[12px] text-[var(--text-muted)]">
              {publication.branch} → {publication.base_branch}
            </p>
            <Checks publication={publication} />
            <a
              className="inline-block text-[12px] underline"
              href={publication.pr_url}
              target="_blank"
              rel="noreferrer"
            >
              Open pull request on GitHub
            </a>
            <p className="text-[11px] text-[var(--text-muted)]">
              PatchPilot does not merge. Your team&apos;s normal review and branch
              protection apply.
            </p>
          </div>
        )}
      </div>
    </Panel>
  );
}

export function ReviewPanel({
  review,
  busy,
  error,
  onApprove,
  onRequestRevision,
  onReject,
  onPublish,
  onSelectVersion,
}: {
  review: Review;
  busy: string | null;
  error: string | null;
  onApprove: (feedback: string) => void;
  onRequestRevision: (feedback: string) => void;
  onReject: (feedback: string) => void;
  onPublish: () => void;
  onSelectVersion: (version: number) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const state = review.review_state;
  const current = review.current;
  const disabled = busy !== null;

  return (
    <div className="space-y-4">
      <Panel
        title="Human review"
        actions={<Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>}
      >
        {!current && (
          <p className="px-4 py-3 text-[13px] text-[var(--text-muted)]">
            This run has not produced a patch to review.
          </p>
        )}

        {current && (
          <div className="space-y-4 px-4 py-3">
            <div className="flex flex-wrap items-center gap-3 text-[13px]">
              <strong>Patch V{current.version}</strong>
              <span className="text-[var(--text-muted)]">
                {current.files.length} file{current.files.length === 1 ? "" : "s"} ·{" "}
                <span className="text-[var(--color-ok)]">+{current.insertions}</span>{" "}
                <span className="text-[var(--color-bad)]">−{current.deletions}</span>
              </span>
              <span className="font-mono text-[11px] text-[var(--text-muted)]">
                {current.digest.slice(0, 12)}
              </span>
            </div>

            <Verification version={current} />

            {current.root_cause && (
              <div>
                <p className="text-[11px] uppercase text-[var(--text-muted)]">Root cause</p>
                <p className="text-[13px]">{current.root_cause}</p>
              </div>
            )}
            {current.repair_summary && (
              <div>
                <p className="text-[11px] uppercase text-[var(--text-muted)]">Repair</p>
                <p className="text-[13px]">{current.repair_summary}</p>
              </div>
            )}

            {review.versions.length > 1 && (
              <div>
                <p className="text-[11px] uppercase text-[var(--text-muted)]">Versions</p>
                <ul className="mt-1 space-y-1 text-[12px]">
                  {review.versions.map((v) => (
                    <li key={v.version} className="flex items-center gap-2">
                      <button
                        type="button"
                        className="underline"
                        onClick={() => onSelectVersion(v.version)}
                      >
                        Patch V{v.version}
                      </button>
                      {v.decision && (
                        <span className="text-[var(--text-muted)]">
                          — {v.decision.replace(/_/g, " ").toLowerCase()}
                        </span>
                      )}
                      {v.reviewer_feedback && (
                        <span className="truncate text-[var(--text-muted)]">
                          “{v.reviewer_feedback}”
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {state === "PATCH_READY" && (
              <div className="space-y-2">
                <label className="text-[11px] uppercase text-[var(--text-muted)]">
                  Reviewer feedback (required to request a revision)
                </label>
                <Textarea
                  rows={4}
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  placeholder={
                    "This fixes the immediate case, but the sibling Hessenberg method " +
                    "has the same indexing bug — please handle both."
                  }
                />
                {error && <p className="text-[12px] text-[var(--color-bad)]">{error}</p>}
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="ghost"
                    disabled={disabled}
                    onClick={() => onReject(feedback)}
                  >
                    {busy === "reject" ? "Rejecting…" : "Reject"}
                  </Button>
                  <Button
                    variant="secondary"
                    disabled={disabled || feedback.trim().length === 0}
                    onClick={() => onRequestRevision(feedback)}
                  >
                    {busy === "revision" ? "Sending…" : "Request Revision"}
                  </Button>
                  <Button
                    variant="primary"
                    disabled={disabled || !review.can_approve || !review.publishing_available}
                    onClick={() => onApprove(feedback)}
                    title={review.publishing_unavailable_reason ?? undefined}
                  >
                    {busy === "approve" ? "Approving…" : "Approve & Create PR"}
                  </Button>
                </div>
                {!review.publishing_available && (
                  <p className="text-[12px] text-[var(--color-warn)]">
                    {review.publishing_unavailable_reason} — you can still review and
                    request revisions; only publication is unavailable.
                  </p>
                )}
                <p className="text-[11px] text-[var(--text-muted)]">
                  Approving creates a branch, commit and pull request from this exact
                  patch. It does not merge anything.
                </p>
              </div>
            )}

            {(state === "APPROVED" || state === "PUBLISH_FAILED") && (
              <div className="space-y-2">
                {error && <p className="text-[12px] text-[var(--color-bad)]">{error}</p>}
                <p className="text-[12px] text-[var(--text-muted)]">
                  Approved{review.approved_by ? ` by ${review.approved_by}` : ""}
                  {review.approved_at
                    ? ` at ${new Date(review.approved_at).toLocaleString()}`
                    : ""}
                  .
                </p>
                <Button variant="primary" disabled={disabled} onClick={onPublish}>
                  {busy === "publish"
                    ? "Publishing…"
                    : state === "PUBLISH_FAILED"
                      ? "Retry publication"
                      : "Create branch & pull request"}
                </Button>
              </div>
            )}

            {state === "REVISION_REQUESTED" && (
              <p className="text-[13px] text-[var(--text-muted)]">
                Queued for revision. The agent will restore this patch, apply your
                feedback and return a new version for review.
              </p>
            )}
          </div>
        )}
      </Panel>

      {review.publication && <PublicationPanel publication={review.publication} />}
    </div>
  );
}
