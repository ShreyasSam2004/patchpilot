"""Review and publication endpoints.

Every route is a named domain action -- approve, request a revision, reject,
publish. There is deliberately no generic escape hatch: no endpoint takes a git
command, a branch name, a repository or a patch from the caller. The frontend
can only ask for transitions the domain already knows how to make, against the
repository and patch the run itself recorded.

Concurrency is handled with a row lock on the run for the whole transaction, so
two approvals or two publish requests serialise instead of racing. Combined
with the unique constraint on ``(run_id, patch_digest)``, a double-clicked
button cannot produce two branches or two pull requests.
"""

from __future__ import annotations

from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...db.models import Event, PublicationRow, Run
from ...db.session import get_session
from ...domain.enums import EventType, ReviewState, RunState
from ...errors import PatchPilotError
from ...logging_setup import get_logger
from ...publish import service as publish_service
from ...publish.credentials import GitHubCredentials
from ...review import service as review_service
from ...review.states import InvalidReviewTransition
from ...runs import service as run_service
from ..schemas import (
    PatchVersionOut,
    PublicationOut,
    ReviewAuditOut,
    ReviewDecisionIn,
    ReviewOut,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/runs", tags=["review"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _locked_run(session: AsyncSession, run_id: str) -> Run:
    """Fetch a run for update.

    ``with_for_update`` serialises concurrent decisions on PostgreSQL. SQLite
    has no row locks, but its single-writer transaction gives the same
    guarantee for the write that follows, so the behaviour matches on both.
    """
    run = await run_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        await session.execute(sa.select(Run.id).where(Run.id == run.id).with_for_update())
    return run


async def _publication(session: AsyncSession, run: Run) -> PublicationRow | None:
    return (
        await session.execute(
            sa.select(PublicationRow)
            .where(PublicationRow.run_id == run.id)
            .order_by(PublicationRow.id.desc())
            .limit(1)
        )
    ).scalars().first()


def _github_availability(settings: Settings, run: Run) -> tuple[bool, str | None]:
    """Whether this run could be published, and why not if it cannot.

    Reported up front so the UI can disable the button with a reason instead of
    letting a reviewer approve and then discover publication was never possible.
    """
    if not GitHubCredentials(settings).configured:
        return False, "GitHub integration is not configured."
    if not (run.repo_owner and run.repo_name):
        return False, "This run has no GitHub repository recorded."
    if not run.base_commit:
        return False, "This run did not record the base commit it was prepared from."
    return True, None


async def _build_review(
    session: AsyncSession, run: Run, settings: Settings, *, include_diff: bool = True
) -> ReviewOut:
    versions = await review_service.list_versions(session, run.id)
    audit = await review_service.list_audit(session, run.id)
    publication = await _publication(session, run)
    state = ReviewState(run.review_state)
    available, reason = _github_availability(settings, run)
    current = versions[-1] if versions else None

    return ReviewOut(
        run_id=run.id,
        code=run.code,
        repository=run.repository,
        repo_owner=run.repo_owner,
        repo_name=run.repo_name,
        base_branch=run.base_branch,
        base_commit=run.base_commit,
        issue_title=run.issue_title,
        issue_url=run.issue_url,
        model=run.model,
        run_state=run.state,
        review_state=str(state),
        approved_patch_sha256=run.approved_patch_sha256,
        approved_at=run.approved_at,
        approved_by=run.approved_by,
        can_approve=state is ReviewState.PATCH_READY and current is not None,
        can_publish=state in {ReviewState.APPROVED, ReviewState.PUBLISH_FAILED},
        publishing_available=available,
        publishing_unavailable_reason=reason,
        current=PatchVersionOut.of(current, include_diff=include_diff) if current else None,
        versions=[PatchVersionOut.of(v) for v in versions],
        publication=PublicationOut.of(publication) if publication else None,
        audit=[ReviewAuditOut.model_validate(a) for a in audit],
    )


async def _emit(session: AsyncSession, run: Run, event: EventType, **payload: Any) -> None:
    """Append a review event to the run's own stream.

    Uses the existing append-only event table directly rather than the agent's
    in-process bus: review actions happen in the API process, long after the
    orchestrator that owned that bus has gone.
    """
    run.last_seq = (run.last_seq or 0) + 1
    session.add(
        Event(run_id=run.id, seq=run.last_seq, type=str(event), iteration=0, payload=payload)
    )


# ----------------------------------------------------------------- read


@router.get("/{run_id}/review", response_model=ReviewOut)
async def get_review(run_id: str, session: SessionDep, settings: SettingsDep) -> ReviewOut:
    run = await run_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return await _build_review(session, run, settings)


@router.get("/{run_id}/review/versions/{version}", response_model=PatchVersionOut)
async def get_version(run_id: str, version: int, session: SessionDep) -> PatchVersionOut:
    """One historical patch version, with its diff, so earlier proposals stay
    inspectable after a revision."""
    run = await run_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    for row in await review_service.list_versions(session, run.id):
        if row.version == version:
            return PatchVersionOut.of(row, include_diff=True)
    raise HTTPException(status_code=404, detail=f"patch version {version} not found")


@router.get("/{run_id}/publication", response_model=PublicationOut)
async def get_publication(
    run_id: str, session: SessionDep, settings: SettingsDep
) -> PublicationOut:
    """Publication state, refreshing PR status and CI checks when possible."""
    run = await run_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    publication = await _publication(session, run)
    if publication is None:
        raise HTTPException(status_code=404, detail="this run has not been published")

    service = publish_service.build_service(settings)
    try:
        await service.refresh_status(session, publication)
    finally:
        await service.host.close()
    return PublicationOut.of(publication)


# ----------------------------------------------------------------- decisions


@router.post("/{run_id}/review/approve", response_model=ReviewOut)
async def approve(
    run_id: str, body: ReviewDecisionIn, session: SessionDep, settings: SettingsDep
) -> ReviewOut:
    """Approve this exact patch for a branch, commit, push and pull request.

    Approval is not a merge. It authorises publication of the reviewed bytes and
    nothing further; the pull request then follows the team's normal process.
    """
    run = await _locked_run(session, run_id)
    try:
        version = await review_service.approve(
            session, run, expected_digest=body.expected_digest, actor=body.reviewer
        )
    except InvalidReviewTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PatchPilotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _emit(
        session, run, EventType.PATCH_APPROVED,
        version=version.version, digest=version.digest, reviewer=body.reviewer,
    )
    return await _build_review(session, run, settings)


@router.post("/{run_id}/review/revision", response_model=ReviewOut)
async def request_revision(
    run_id: str, body: ReviewDecisionIn, session: SessionDep, settings: SettingsDep
) -> ReviewOut:
    """Send the patch back to the agent with feedback, and requeue the run."""
    run = await _locked_run(session, run_id)
    try:
        version = await review_service.request_revision(
            session, run, feedback=body.feedback, actor=body.reviewer
        )
    except InvalidReviewTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PatchPilotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Back into the same queue the run came from. The worker sees
    # REVISION_REQUESTED, restores this patch and passes the feedback through.
    run.state = str(RunState.PENDING)
    run.finished_at = None
    run.worker_id = None
    run.heartbeat_at = None

    await _emit(
        session, run, EventType.PATCH_REVISION_REQUESTED,
        version=version.version, reviewer=body.reviewer, feedback=body.feedback[:2000],
    )
    return await _build_review(session, run, settings)


@router.post("/{run_id}/review/reject", response_model=ReviewOut)
async def reject(
    run_id: str, body: ReviewDecisionIn, session: SessionDep, settings: SettingsDep
) -> ReviewOut:
    """Reject the patch. Terminal: nothing is published and the run is done."""
    run = await _locked_run(session, run_id)
    try:
        version = await review_service.reject(
            session, run, feedback=body.feedback, actor=body.reviewer
        )
    except InvalidReviewTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await _emit(
        session, run, EventType.PATCH_REJECTED,
        version=version.version if version else None, reviewer=body.reviewer,
    )
    return await _build_review(session, run, settings)


# ----------------------------------------------------------------- publish


@router.post("/{run_id}/publish", response_model=PublicationOut)
async def publish(
    run_id: str, body: ReviewDecisionIn, session: SessionDep, settings: SettingsDep
) -> PublicationOut:
    """Create the branch, commit, push and pull request for the approved patch.

    Idempotent: a repeated call returns the existing pull request rather than
    opening a second one, and a call after a partial failure resumes from the
    last completed step.
    """
    run = await _locked_run(session, run_id)
    state = ReviewState(run.review_state)

    if state is ReviewState.PR_CREATED:
        publication = await _publication(session, run)
        if publication is not None:
            return PublicationOut.of(publication)

    if state not in {ReviewState.APPROVED, ReviewState.PUBLISHING, ReviewState.PUBLISH_FAILED}:
        raise HTTPException(
            status_code=409,
            detail=f"cannot publish from review state {state}; approve the patch first",
        )

    service = publish_service.build_service(settings)
    try:
        await _emit(session, run, EventType.PUBLICATION_STARTED, reviewer=body.reviewer)
        publication = await service.publish(session, run, actor=body.reviewer)
    finally:
        await service.host.close()

    if publication.pr_number:
        await _emit(
            session, run, EventType.GITHUB_PULL_REQUEST_CREATED,
            pr_number=publication.pr_number, pr_url=publication.pr_url,
            branch=publication.branch,
        )
    else:
        await _emit(
            session, run, EventType.PUBLICATION_FAILED,
            code=publication.error_code, detail=(publication.error_detail or "")[:500],
        )
    return PublicationOut.of(publication)
