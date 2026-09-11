"""Human review of a generated patch.

Sits beside :mod:`patchpilot.runs.service` in the application layer: the API
routers and the worker both call in here, and neither reaches into the ORM to
change review state on its own. Three rules are enforced in this one place, so
there is no second path around them:

* every state change goes through the transition guard;
* approval binds to a digest, and any later patch invalidates it;
* every decision writes an audit row before the transaction commits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.result import AgentResult
from ..db.models import PatchVersionRow, ReviewAuditRow, Run
from ..domain.enums import ReviewAction, ReviewState
from ..errors import PatchPilotError
from ..logging_setup import get_logger
from ..repair.patchquality import assess_patch
from .digest import digests_match, patch_digest
from .states import ensure_transition

log = get_logger(__name__)


class ReviewError(PatchPilotError):
    """A review action that cannot be performed as asked."""


async def record_audit(
    session: AsyncSession,
    run: Run,
    action: ReviewAction,
    *,
    version: int | None = None,
    actor: str | None = None,
    feedback: str = "",
    digest: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit row. Shared with :mod:`patchpilot.publish`,
    which records the publication half of the same trail."""
    session.add(
        ReviewAuditRow(
            run_id=run.id,
            patch_version=version,
            action=str(action),
            actor=actor,
            feedback=feedback,
            patch_digest=digest,
            detail=detail or {},
        )
    )


async def _set_state(session: AsyncSession, run: Run, target: ReviewState) -> ReviewState:
    current = ReviewState(run.review_state)
    run.review_state = str(ensure_transition(current, target))
    return ReviewState(run.review_state)


async def latest_version(session: AsyncSession, run_id: str) -> PatchVersionRow | None:
    return (
        await session.execute(
            sa.select(PatchVersionRow)
            .where(PatchVersionRow.run_id == run_id)
            .order_by(PatchVersionRow.version.desc())
            .limit(1)
        )
    ).scalars().first()


async def list_versions(session: AsyncSession, run_id: str) -> list[PatchVersionRow]:
    return list(
        (
            await session.execute(
                sa.select(PatchVersionRow)
                .where(PatchVersionRow.run_id == run_id)
                .order_by(PatchVersionRow.version)
            )
        ).scalars().all()
    )


async def list_audit(session: AsyncSession, run_id: str) -> list[ReviewAuditRow]:
    return list(
        (
            await session.execute(
                sa.select(ReviewAuditRow)
                .where(ReviewAuditRow.run_id == run_id)
                .order_by(ReviewAuditRow.id)
            )
        ).scalars().all()
    )


async def record_generated_patch(
    session: AsyncSession, run: Run, result: AgentResult
) -> PatchVersionRow | None:
    """Turn a finished agent run into the next patch version for review.

    Called from the worker once per run completion. Returns ``None`` when the
    run produced nothing worth reviewing -- an empty diff, or one containing
    only debug artefacts -- in which case the run never enters the review
    workflow at all and no reviewer is asked to look at nothing.
    """
    assessment = assess_patch(result.final_patch)
    if not assessment.is_repair:
        log.info(
            "review.no_patch_to_review",
            run_id=run.id,
            quality=str(assessment.quality),
        )
        return None

    previous = await latest_version(session, run.id)
    version = (previous.version + 1) if previous else 1
    digest = patch_digest(result.final_patch)

    if previous is not None and previous.digest == digest:
        # A re-run that produced a byte-identical patch. Reusing the existing
        # version keeps any decision already recorded against it, instead of
        # silently resetting the reviewer's work.
        log.info("review.identical_patch", run_id=run.id, version=previous.version)
        return previous

    last_test = result.test_runs[-1] if result.test_runs else None
    row = PatchVersionRow(
        run_id=run.id,
        version=version,
        diff=result.final_patch,
        digest=digest,
        # From the patch, not from the run. `files_modified` is everything the
        # agent touched, including scratch work that the submitted diff excludes,
        # and the per-attempt counts sum every attempt rather than describing the
        # diff that is actually up for review. A reviewer deciding whether to
        # publish needs the shape of the bytes in front of them.
        files=list(assessment.changed_files),
        insertions=assessment.added_lines,
        deletions=assessment.removed_lines,
        patch_quality=str(assessment.quality),
        verification={
            "passed": last_test.passed if last_test else 0,
            "failed": last_test.failed if last_test else 0,
            "errors": last_test.errors if last_test else 0,
            "framework": last_test.framework if last_test else "unknown",
            "green": bool(last_test.green) if last_test else False,
            "summary_recognized": bool(last_test.summary_recognized) if last_test else False,
            "verified_by_tests": bool(result.metrics.verified_by_tests),
            "command": last_test.command if last_test else "",
        },
        model=run.model,
        iterations=result.metrics.iterations,
        root_cause=(result.hypotheses[-1].suspected_cause if result.hypotheses else ""),
        repair_summary=result.summary or result.stop_reason,
    )
    session.add(row)

    # A new patch means anything approved earlier no longer describes the run.
    if run.approved_patch_sha256 and run.approved_patch_sha256 != digest:
        await record_audit(
            session,
            run,
            ReviewAction.APPROVAL_INVALIDATED,
            version=version,
            digest=run.approved_patch_sha256,
            detail={"reason": "a newer patch version was generated"},
        )
        run.approved_patch_sha256 = None
        run.approved_at = None
        run.approved_by = None

    revising = ReviewState(run.review_state) is ReviewState.REVISING
    await _set_state(session, run, ReviewState.PATCH_READY)
    await record_audit(
        session,
        run,
        ReviewAction.PATCH_REVISED if revising else ReviewAction.PATCH_GENERATED,
        version=version,
        digest=digest,
    )
    log.info("review.patch_ready", run_id=run.id, version=version, digest=digest[:12])
    return row


async def begin_revision(session: AsyncSession, run: Run) -> None:
    """Mark a run as being reworked. Called by the worker as it picks it up."""
    await _set_state(session, run, ReviewState.REVISING)


async def request_revision(
    session: AsyncSession, run: Run, *, feedback: str, actor: str | None = None
) -> PatchVersionRow:
    """Send the current patch back to the agent with reviewer feedback."""
    text = (feedback or "").strip()
    if not text:
        raise ReviewError("reviewer feedback is required when requesting a revision")

    version = await latest_version(session, run.id)
    if version is None:
        raise ReviewError("there is no patch to revise")

    await _set_state(session, run, ReviewState.REVISION_REQUESTED)
    version.decision = str(ReviewAction.REVISION_REQUESTED)
    version.reviewer_feedback = text
    version.decided_at = datetime.now(UTC)
    version.decided_by = actor

    # Feedback reaches the agent through the run's own config, which the worker
    # already reads when building the AgentTask -- the same seam the CLI uses
    # for operator guidance, rather than a second prompt-injection path.
    config = dict(run.config or {})
    config["reviewer_feedback"] = text
    config["revision_of_version"] = version.version
    run.config = config

    if run.approved_patch_sha256:
        run.approved_patch_sha256 = None
        run.approved_at = None
        run.approved_by = None

    await record_audit(
        session,
        run,
        ReviewAction.REVISION_REQUESTED,
        version=version.version,
        actor=actor,
        feedback=text,
        digest=version.digest,
    )
    log.info("review.revision_requested", run_id=run.id, version=version.version)
    return version


async def approve(
    session: AsyncSession,
    run: Run,
    *,
    expected_digest: str | None = None,
    actor: str | None = None,
) -> PatchVersionRow:
    """Approve the latest patch version for branch, commit, push and PR.

    Idempotent: approving an already-approved, unchanged patch returns the same
    version rather than erroring, because that is what a double-clicked button
    looks like.

    ``expected_digest`` is the digest the reviewer was actually looking at. When
    supplied and stale, the approval is refused -- the patch changed between the
    page rendering and the click, and what the reviewer read is not what they
    would be approving.
    """
    version = await latest_version(session, run.id)
    if version is None:
        raise ReviewError("there is no patch to approve")
    if not version.diff.strip():
        raise ReviewError("the latest patch version is empty")

    if expected_digest and expected_digest != version.digest:
        raise ReviewError(
            "this patch changed since it was displayed; reload and review the "
            "current version before approving"
        )

    if ReviewState(run.review_state) is ReviewState.APPROVED and digests_match(
        run.approved_patch_sha256 or "", version.digest
    ):
        return version

    await _set_state(session, run, ReviewState.APPROVED)
    run.approved_patch_sha256 = version.digest
    run.approved_at = datetime.now(UTC)
    run.approved_by = actor
    version.decision = str(ReviewAction.PATCH_APPROVED)
    version.decided_at = run.approved_at
    version.decided_by = actor

    await record_audit(
        session,
        run,
        ReviewAction.PATCH_APPROVED,
        version=version.version,
        actor=actor,
        digest=version.digest,
        detail={"repository": f"{run.repo_owner}/{run.repo_name}" if run.repo_owner else None},
    )
    log.info(
        "review.approved", run_id=run.id, version=version.version, digest=version.digest[:12]
    )
    return version


async def reject(
    session: AsyncSession, run: Run, *, feedback: str = "", actor: str | None = None
) -> PatchVersionRow | None:
    """Reject the patch. Terminal: the run is done and nothing is published."""
    version = await latest_version(session, run.id)
    await _set_state(session, run, ReviewState.REJECTED)
    run.approved_patch_sha256 = None
    run.approved_at = None
    run.approved_by = None
    if version is not None:
        version.decision = str(ReviewAction.PATCH_REJECTED)
        version.reviewer_feedback = (feedback or "").strip()
        version.decided_at = datetime.now(UTC)
        version.decided_by = actor
    await record_audit(
        session,
        run,
        ReviewAction.PATCH_REJECTED,
        version=version.version if version else None,
        actor=actor,
        feedback=(feedback or "").strip(),
        digest=version.digest if version else None,
    )
    log.info("review.rejected", run_id=run.id)
    return version


async def approved_version(session: AsyncSession, run: Run) -> PatchVersionRow:
    """The version whose digest the run currently has approved.

    Raises when nothing is approved or when the stored digest no longer matches
    any version -- the two situations in which publishing would push something
    no human signed off on.
    """
    if not run.approved_patch_sha256:
        raise ReviewError("no patch has been approved for this run")
    row = (
        await session.execute(
            sa.select(PatchVersionRow).where(
                PatchVersionRow.run_id == run.id,
                PatchVersionRow.digest == run.approved_patch_sha256,
            )
        )
    ).scalars().first()
    if row is None:
        raise ReviewError("the approved patch is no longer present on this run")
    if patch_digest(row.diff) != run.approved_patch_sha256:
        raise ReviewError("the stored patch does not match its approved digest")
    return row
