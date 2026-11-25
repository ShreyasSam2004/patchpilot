"""Run lifecycle service.

Everything that creates, queries or finalises a :class:`Run` row lives here, so
the API routers stay thin and the worker and CLI reuse the same logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.result import AgentResult
from ..agent.session import SessionConfig
from ..config import Settings, get_settings
from ..db.models import Event, PatchRow, Run, TestRunRow, ToolCallRow
from ..domain.enums import ReviewState, RunState
from ..ids import new_id, short_code
from ..logging_setup import get_logger
from ..publish.identity import parse_repository
from ..review import service as review_service

log = get_logger(__name__)


async def create_run(
    session: AsyncSession,
    *,
    issue: str,
    repository: str | None = None,
    repo_url: str | None = None,
    local_path: str | None = None,
    base_commit: str | None = None,
    issue_url: str | None = None,
    issue_title: str | None = None,
    test_command: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    docker_image: str | None = None,
    max_iterations: int | None = None,
    max_tool_calls: int | None = None,
    max_runtime_s: int | None = None,
    settings: Settings | None = None,
) -> Run:
    """Queue a new run. The worker picks it up; this never blocks."""
    settings = settings or get_settings()
    config = SessionConfig(
        provider=provider, model=model, sandbox=sandbox, docker_image=docker_image
    )

    budget_overrides = {
        key: value
        for key, value in {
            "max_iterations": max_iterations,
            "max_tool_calls": max_tool_calls,
            "max_runtime_s": max_runtime_s,
        }.items()
        if value
    }

    # Fixed now, from the URL the user actually supplied. Deciding the push
    # target later -- from issue text, a branch name, anything the model saw --
    # is how an agent opens a pull request against the wrong repository.
    identity = parse_repository(repo_url)

    run = Run(
        id=new_id(),
        code=short_code(),
        repository=repository or _infer_repository(repo_url, local_path),
        repo_url=repo_url,
        local_path=local_path,
        base_commit=base_commit,
        issue=issue,
        issue_url=issue_url,
        issue_title=issue_title,
        test_command=test_command,
        provider=provider or settings.provider,
        model=model or settings.model,
        sandbox=sandbox or settings.sandbox,
        config={**config.describe(settings), "budget_overrides": budget_overrides},
        state=str(RunState.PENDING),
        review_state=str(ReviewState.RUNNING),
        repo_provider=identity.provider if identity else None,
        repo_owner=identity.owner if identity else None,
        repo_name=identity.repo if identity else None,
        queued_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    log.info("run.created", run_id=run.id, code=run.code, repository=run.repository)
    return run


def _infer_repository(repo_url: str | None, local_path: str | None) -> str:
    if repo_url:
        cleaned = repo_url.rstrip("/").removesuffix(".git")
        parts = cleaned.split("/")
        return "/".join(parts[-2:]) if len(parts) >= 2 else cleaned
    if local_path:
        return Path(local_path).name
    return "unknown"


async def get_run(session: AsyncSession, run_id: str) -> Run | None:
    """Look up a run by full id or by its short display code."""
    run = await session.get(Run, run_id)
    if run is not None:
        return run
    result = await session.execute(
        sa.select(Run).where(Run.code == run_id.upper()).order_by(Run.queued_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def list_runs(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    state: str | None = None,
    exclude_benchmark: bool = False,
) -> tuple[list[Run], int]:
    """Recent runs, newest first, with a total count for pagination."""
    query = sa.select(Run)
    count_query = sa.select(sa.func.count()).select_from(Run)
    if state:
        query = query.where(Run.state == state)
        count_query = count_query.where(Run.state == state)
    if exclude_benchmark:
        query = query.where(Run.benchmark_task_id.is_(None))
        count_query = count_query.where(Run.benchmark_task_id.is_(None))

    query = query.order_by(Run.queued_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(query)).scalars().all()
    total = (await session.execute(count_query)).scalar_one()
    return list(rows), int(total)


async def get_events(
    session: AsyncSession, run_id: str, *, after_seq: int = 0, limit: int = 2000
) -> list[Event]:
    result = await session.execute(
        sa.select(Event)
        .where(Event.run_id == run_id, Event.seq > after_seq)
        .order_by(Event.seq)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_tool_calls(session: AsyncSession, run_id: str) -> list[ToolCallRow]:
    result = await session.execute(
        sa.select(ToolCallRow).where(ToolCallRow.run_id == run_id).order_by(ToolCallRow.id)
    )
    return list(result.scalars().all())


async def get_test_runs(session: AsyncSession, run_id: str) -> list[TestRunRow]:
    result = await session.execute(
        sa.select(TestRunRow).where(TestRunRow.run_id == run_id).order_by(TestRunRow.id)
    )
    return list(result.scalars().all())


async def get_patches(session: AsyncSession, run_id: str) -> list[PatchRow]:
    result = await session.execute(
        sa.select(PatchRow).where(PatchRow.run_id == run_id).order_by(PatchRow.id)
    )
    return list(result.scalars().all())


async def request_cancel(session: AsyncSession, run_id: str) -> bool:
    """Ask a job to stop.

    A run still in the queue is cancelled outright. Merely flagging it let a
    worker claim it, clone the repository, start the agent and pay for model
    calls until its first heartbeat noticed. The update is conditional on the
    run still being PENDING, so it cannot race a worker's claim: whichever
    lands first wins, and a claimed run falls through to the flag, which the
    worker honours each iteration.
    """
    run = await get_run(session, run_id)
    if run is None or RunState(run.state).is_terminal:
        return False

    if RunState(run.state) is RunState.PENDING:
        claimed_first = cast(
            "CursorResult[Any]",
            await session.execute(
                sa.update(Run)
                .where(Run.id == run.id, Run.state == str(RunState.PENDING))
                .values(
                    state=str(RunState.CANCELLED),
                    cancel_requested=True,
                    stop_reason="cancelled before it started",
                    finished_at=datetime.now(UTC),
                )
            ),
        )
        if claimed_first.rowcount == 1:
            await session.refresh(run)
            return True
        await session.refresh(run)  # a worker claimed it meanwhile

    run.cancel_requested = True
    return True


async def apply_result(session: AsyncSession, run_id: str, result: AgentResult) -> None:
    """Write the agent's outcome onto the run row."""
    run = await session.get(Run, run_id)
    if run is None:
        log.warning("run.missing_on_finalize", run_id=run_id)
        return

    run.state = str(result.state)
    run.stop_reason = result.stop_reason
    run.summary = result.summary
    run.failure_class = str(result.failure_class) if result.failure_class else None
    run.error = result.error
    run.final_patch = result.final_patch
    run.files_inspected = result.files_inspected
    run.files_modified = result.files_modified
    run.metrics = result.metrics.to_dict()
    run.finished_at = result.finished_at or datetime.now(UTC)

    # Record the revision actually prepared, so the patch can be published
    # against a known base. Never overwrite a commit the caller pinned.
    if result.base_commit and not run.base_commit:
        run.base_commit = result.base_commit
    if result.base_branch and not run.base_branch:
        run.base_branch = result.base_branch

    # Offer the patch for human review. Returns None when there is nothing
    # worth reviewing, in which case the run never enters the review workflow.
    await review_service.record_generated_patch(session, run, result)

    if result.final_patch.strip():
        session.add(
            PatchRow(
                run_id=run_id,
                iteration=result.metrics.iterations,
                diff=result.final_patch,
                files=result.files_modified,
                insertions=sum(p.insertions for p in result.patch_attempts),
                deletions=sum(p.deletions for p in result.patch_attempts),
                applied=True,
                is_final=True,
            )
        )


async def summary_stats(session: AsyncSession) -> dict[str, Any]:
    """Counts for the dashboard header."""
    rows = (
        await session.execute(sa.select(Run.state, sa.func.count()).group_by(Run.state))
    ).all()
    by_state = {state: int(count) for state, count in rows}
    total = sum(by_state.values())
    resolved = by_state.get(str(RunState.RESOLVED), 0)
    finished = sum(
        by_state.get(str(s), 0)
        for s in (RunState.RESOLVED, RunState.FAILED, RunState.TIMEOUT, RunState.CANCELLED)
    )
    return {
        "total": total,
        "by_state": by_state,
        "active": total - finished,
        "resolved": resolved,
        "agent_success_rate": round(resolved / finished * 100, 1) if finished else None,
    }
