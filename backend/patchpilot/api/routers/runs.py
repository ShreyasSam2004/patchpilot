"""Run endpoints, including the SSE live stream."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from ...config import Settings, get_settings
from ...db.session import get_session, session_scope
from ...diffutil import diff_to_dict
from ...domain.enums import RunState
from ...fixtures import get_fixture, list_fixtures
from ...logging_setup import get_logger
from ...runs import service
from ..schemas import (
    CreateRunRequest,
    DiffOut,
    EventOut,
    HypothesisOut,
    PatchOut,
    RunDetailOut,
    RunListOut,
    RunSummaryOut,
    TestRunOut,
    ToolCallOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("", response_model=RunDetailOut, status_code=201)
async def create_run(
    payload: CreateRunRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RunDetailOut:
    """Queue an autonomous bug-fixing run.

    Returns immediately: the worker executes the run in the background and the
    client follows progress on ``/api/runs/{id}/events``.
    """
    local_path = payload.local_path
    repository = payload.repository
    test_command = payload.test_command
    issue = payload.issue

    if payload.fixture:
        try:
            fixture = get_fixture(payload.fixture)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        local_path = str(fixture.repo_path)
        repository = repository or fixture.repository
        test_command = test_command or fixture.test_command
        if not issue.strip():
            issue = fixture.issue

    if local_path:
        resolved = Path(local_path).expanduser().resolve()
        if not resolved.exists():
            raise HTTPException(status_code=400, detail=f"local path not found: {resolved}")
        local_path = str(resolved)

    run = await service.create_run(
        session,
        issue=issue,
        repository=repository,
        repo_url=payload.repo_url,
        local_path=local_path,
        base_commit=payload.base_commit,
        issue_url=payload.issue_url,
        issue_title=payload.issue_title,
        test_command=test_command,
        provider=payload.provider,
        model=payload.model,
        sandbox=payload.sandbox,
        max_iterations=payload.max_iterations,
        max_tool_calls=payload.max_tool_calls,
        max_runtime_s=payload.max_runtime_s,
        settings=settings,
    )
    return RunDetailOut.of(run)


@router.get("", response_model=RunListOut)
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    state: str | None = None,
    exclude_benchmark: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
) -> RunListOut:
    runs, total = await service.list_runs(
        session, limit=limit, offset=offset, state=state, exclude_benchmark=exclude_benchmark
    )
    return RunListOut(
        items=[RunSummaryOut.of(r) for r in runs], total=total, limit=limit, offset=offset
    )


@router.get("/fixtures")
async def get_fixtures() -> list[dict[str, Any]]:
    """Built-in fixture bug tasks, for the 'try it' path in the UI."""
    return [fixture.to_dict() for fixture in list_fixtures()]


async def _require_run(session: AsyncSession, run_id: str):
    run = await service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> RunDetailOut:
    return RunDetailOut.of(await _require_run(session, run_id))


@router.get("/{run_id}/events", response_model=list[EventOut])
async def get_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    run = await _require_run(session, run_id)
    events = await service.get_events(session, run.id, after_seq=after, limit=limit)
    return [EventOut.model_validate(e) for e in events]


@router.get("/{run_id}/stream")
async def stream_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    settings: Settings = Depends(get_settings),
) -> EventSourceResponse:
    """Server-Sent Events stream of a run's events.

    Streams from the database rather than from in-memory state, so a browser
    that reconnects with ``?after=<last seq>`` resumes exactly where it stopped,
    and a finished run replays identically. That also means the API process
    does not have to be the process that executed the run.
    """

    async def publish():
        cursor = after
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break

            async with session_scope(settings) as session:
                run = await service.get_run(session, run_id)
                if run is None:
                    yield {"event": "error", "data": json.dumps({"detail": "run not found"})}
                    return
                events = await service.get_events(session, run.id, after_seq=cursor, limit=200)
                terminal = RunState(run.state).is_terminal

            for event in events:
                cursor = event.seq
                yield {
                    "event": "message",
                    "id": str(event.seq),
                    "data": json.dumps(
                        {
                            "seq": event.seq,
                            "type": event.type,
                            "iteration": event.iteration,
                            "payload": event.payload,
                            "created_at": event.created_at.isoformat(),
                        }
                    ),
                }

            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if terminal:
                    yield {"event": "done", "data": json.dumps({"state": run.state})}
                    return
                if idle_ticks % 20 == 0:
                    yield {"event": "ping", "data": "{}"}

            await asyncio.sleep(0.5)

    return EventSourceResponse(publish())


@router.get("/{run_id}/diff", response_model=DiffOut)
async def get_diff(run_id: str, session: AsyncSession = Depends(get_session)) -> DiffOut:
    run = await _require_run(session, run_id)
    return DiffOut.model_validate(diff_to_dict(run.final_patch or ""))


@router.get("/{run_id}/patches", response_model=list[PatchOut])
async def get_patches(run_id: str, session: AsyncSession = Depends(get_session)) -> list[PatchOut]:
    run = await _require_run(session, run_id)
    return [PatchOut.model_validate(p) for p in await service.get_patches(session, run.id)]


@router.get("/{run_id}/tests", response_model=list[TestRunOut])
async def get_tests(run_id: str, session: AsyncSession = Depends(get_session)) -> list[TestRunOut]:
    run = await _require_run(session, run_id)
    return [TestRunOut.model_validate(t) for t in await service.get_test_runs(session, run.id)]


@router.get("/{run_id}/tools", response_model=list[ToolCallOut])
async def get_tools(run_id: str, session: AsyncSession = Depends(get_session)) -> list[ToolCallOut]:
    run = await _require_run(session, run_id)
    return [ToolCallOut.model_validate(t) for t in await service.get_tool_calls(session, run.id)]


@router.get("/{run_id}/hypotheses", response_model=list[HypothesisOut])
async def get_hypotheses(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> list[HypothesisOut]:
    run = await _require_run(session, run_id)
    await session.refresh(run, ["hypotheses"])
    return [HypothesisOut.model_validate(h) for h in run.hypotheses]


@router.get("/{run_id}/files")
async def get_files(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Files the agent inspected and modified, for the FILES tab."""
    run = await _require_run(session, run_id)
    inspected = run.files_inspected or []
    modified = set(run.files_modified or [])
    return {
        "inspected": [
            {"path": path, "modified": path in modified}
            for path in sorted(set(inspected) | modified)
        ],
        "modified": sorted(modified),
        "inspected_count": len(set(inspected) | modified),
        "modified_count": len(modified),
    }


@router.post("/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await _require_run(session, run_id)
    ok = await service.request_cancel(session, run.id)
    if not ok:
        raise HTTPException(status_code=409, detail=f"run is already {run.state}")
    return {"cancel_requested": True, "run_id": run.id}
