"""Benchmark endpoints."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...bench import evaluator_preflight
from ...bench.dataset import dataset_available
from ...bench.runner import create_benchmark
from ...config import Settings, get_settings
from ...db.models import BenchmarkRun, BenchmarkTask, Run
from ...db.session import get_session
from ...diffutil import diff_to_dict
from ...domain.enums import BenchmarkRunStatus, BenchmarkStatus
from ...errors import BenchmarkError
from ...logging_setup import get_logger
from ...runs import service as run_service
from ..schemas import (
    BenchmarkListOut,
    BenchmarkOut,
    BenchmarkTaskDetailOut,
    BenchmarkTaskOut,
    CreateBenchmarkRequest,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])


@router.get("/preflight")
async def preflight(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Report honestly whether a real SWE-bench evaluation can run here."""
    checks = await evaluator_preflight(settings)
    dataset_ok, dataset_detail = dataset_available()
    checks["dataset_available"] = dataset_ok
    checks["dataset_detail"] = dataset_detail
    checks["dataset"] = settings.swebench_dataset
    checks["split"] = settings.swebench_split
    if not dataset_ok:
        checks["ready"] = False
        checks["problems"] = [*checks.get("problems", []), dataset_detail]
    return checks


@router.post("", response_model=BenchmarkOut, status_code=201)
async def create(
    payload: CreateBenchmarkRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BenchmarkOut:
    """Queue a SWE-bench batch. The worker generates patches, then evaluates."""
    try:
        benchmark = await create_benchmark(
            session,
            name=payload.name,
            dataset=payload.dataset,
            split=payload.split,
            limit=payload.limit,
            offset=payload.offset,
            instance_ids=payload.instance_ids,
            provider=payload.provider,
            model=payload.model,
            sandbox=payload.sandbox,
            max_iterations=payload.max_iterations,
            evaluate=payload.evaluate,
            settings=settings,
        )
    except BenchmarkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BenchmarkOut.of(benchmark)


@router.get("", response_model=BenchmarkListOut)
async def list_benchmarks(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> BenchmarkListOut:
    rows = (
        await session.execute(
            sa.select(BenchmarkRun).order_by(BenchmarkRun.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    total = (
        await session.execute(sa.select(sa.func.count()).select_from(BenchmarkRun))
    ).scalar_one()
    return BenchmarkListOut(items=[BenchmarkOut.of(b) for b in rows], total=int(total))


@router.get("/leaderboard")
async def leaderboard(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Best genuinely-evaluated result, for the landing page and resume bullet.

    Returns ``null`` when nothing has been evaluated by the official harness.
    The UI must render "no benchmark results yet" rather than a placeholder
    number — a fabricated resolve rate would defeat the purpose of the project.
    """
    rows = (
        await session.execute(
            sa.select(BenchmarkRun)
            .where(
                BenchmarkRun.evaluated.is_(True),
                BenchmarkRun.status == str(BenchmarkRunStatus.COMPLETED),
            )
            .order_by(BenchmarkRun.created_at.desc())
        )
    ).scalars().all()

    graded = [b for b in rows if b.resolve_rate is not None]
    if not graded:
        return {
            "has_results": False,
            "best": None,
            "latest": None,
            "runs": 0,
            "note": (
                "No SWE-bench batch has been graded by the official evaluator yet. "
                "Run one with: patchpilot bench run --limit 10"
            ),
        }

    best = max(graded, key=lambda b: (b.resolve_rate or 0, b.total_tasks))
    latest = graded[0]
    return {
        "has_results": True,
        "runs": len(graded),
        "best": BenchmarkOut.of(best).model_dump(),
        "latest": BenchmarkOut.of(latest).model_dump(),
        "resume_bullet": _resume_bullet(best),
    }


def _resume_bullet(benchmark: BenchmarkRun) -> str:
    """Generate a resume line from real, evaluator-confirmed numbers only."""
    attempted = benchmark.attempted_tasks
    return (
        "Built PatchPilot, an autonomous software-engineering agent using LLM tool-calling, "
        "MCP-compatible repository tools, iterative test-feedback loops, and isolated code "
        f"execution; resolved {benchmark.resolve_rate}% of {attempted} attempted SWE-bench "
        f"Lite tasks ({benchmark.model}), graded by the official SWE-bench harness."
    )


async def _require(session: AsyncSession, benchmark_id: str) -> BenchmarkRun:
    benchmark = await session.get(BenchmarkRun, benchmark_id)
    if benchmark is None:
        raise HTTPException(status_code=404, detail=f"benchmark {benchmark_id} not found")
    return benchmark


@router.get("/{benchmark_id}", response_model=BenchmarkOut)
async def get_benchmark(
    benchmark_id: str, session: AsyncSession = Depends(get_session)
) -> BenchmarkOut:
    return BenchmarkOut.of(await _require(session, benchmark_id))


@router.get("/{benchmark_id}/tasks", response_model=list[BenchmarkTaskOut])
async def get_tasks(
    benchmark_id: str,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[BenchmarkTaskOut]:
    await _require(session, benchmark_id)
    query = sa.select(BenchmarkTask).where(BenchmarkTask.benchmark_id == benchmark_id)
    if status:
        query = query.where(BenchmarkTask.benchmark_status == status)
    rows = (await session.execute(query.order_by(BenchmarkTask.instance_id))).scalars().all()
    return [BenchmarkTaskOut.of(t) for t in rows]


@router.get("/{benchmark_id}/tasks/{task_id}")
async def get_task(
    benchmark_id: str, task_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """One benchmark task with its full agent run attached."""
    await _require(session, benchmark_id)
    task = (
        await session.execute(
            sa.select(BenchmarkTask).where(
                BenchmarkTask.benchmark_id == benchmark_id,
                sa.or_(BenchmarkTask.id == task_id, BenchmarkTask.instance_id == task_id),
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    payload: dict[str, Any] = {
        "task": BenchmarkTaskDetailOut.of(task).model_dump(),
        "diff": diff_to_dict(task.model_patch or ""),
        "run": None,
        "tool_calls": [],
        "test_runs": [],
        "hypotheses": [],
        "events": [],
    }

    if task.run_id:
        run = await session.get(Run, task.run_id)
        if run is not None:
            from ..schemas import (
                EventOut,
                HypothesisOut,
                RunDetailOut,
                TestRunOut,
                ToolCallOut,
            )

            await session.refresh(run, ["hypotheses"])
            payload["run"] = RunDetailOut.of(run).model_dump()
            payload["tool_calls"] = [
                ToolCallOut.model_validate(t).model_dump()
                for t in await run_service.get_tool_calls(session, run.id)
            ]
            payload["test_runs"] = [
                TestRunOut.model_validate(t).model_dump()
                for t in await run_service.get_test_runs(session, run.id)
            ]
            payload["hypotheses"] = [
                HypothesisOut.model_validate(h).model_dump() for h in run.hypotheses
            ]
            payload["events"] = [
                EventOut.model_validate(e).model_dump()
                for e in await run_service.get_events(session, run.id, limit=3000)
            ]
    return payload


@router.get("/{benchmark_id}/predictions")
async def get_predictions(
    benchmark_id: str, session: AsyncSession = Depends(get_session)
) -> list[dict[str, str]]:
    """The exact predictions file contents handed to the official harness."""
    benchmark = await _require(session, benchmark_id)
    rows = (
        await session.execute(
            sa.select(BenchmarkTask)
            .where(BenchmarkTask.benchmark_id == benchmark_id)
            .order_by(BenchmarkTask.instance_id)
        )
    ).scalars().all()
    return [
        {
            "instance_id": t.instance_id,
            "model_name_or_path": benchmark.model,
            "model_patch": t.model_patch or "",
        }
        for t in rows
    ]


@router.post("/{benchmark_id}/evaluate", status_code=202)
async def request_evaluation(
    benchmark_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Re-queue the official evaluation for a batch that has patches but no grade."""
    benchmark = await _require(session, benchmark_id)
    if benchmark.status not in {
        str(BenchmarkRunStatus.COMPLETED),
        str(BenchmarkRunStatus.FAILED),
    }:
        raise HTTPException(
            status_code=409, detail=f"benchmark is currently {benchmark.status}"
        )
    # Lazy relationship loading is not available under async SQLAlchemy; query
    # the tasks explicitly.
    tasks = (
        await session.execute(
            sa.select(BenchmarkTask).where(BenchmarkTask.benchmark_id == benchmark.id)
        )
    ).scalars().all()

    if not any((t.model_patch or "").strip() for t in tasks):
        raise HTTPException(status_code=409, detail="no patches to evaluate")

    # Reset only the graded verdicts. Tasks keep a non-PENDING status, so the
    # runner skips generation and goes straight to evaluation.
    for task in tasks:
        if task.benchmark_status in {
            str(BenchmarkStatus.RESOLVED),
            str(BenchmarkStatus.UNRESOLVED),
        }:
            task.benchmark_status = str(BenchmarkStatus.NOT_EVALUATED)
    benchmark.status = str(BenchmarkRunStatus.PENDING)
    benchmark.evaluated = False
    benchmark.error = None
    benchmark.config = {**(benchmark.config or {}), "evaluate": True}
    return {"queued": True, "benchmark_id": benchmark.id}


@router.post("/{benchmark_id}/cancel", status_code=202)
async def cancel(
    benchmark_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    benchmark = await _require(session, benchmark_id)
    if benchmark.status in {
        str(BenchmarkRunStatus.COMPLETED),
        str(BenchmarkRunStatus.FAILED),
        str(BenchmarkRunStatus.CANCELLED),
    }:
        raise HTTPException(status_code=409, detail=f"benchmark is already {benchmark.status}")
    benchmark.cancel_requested = True
    return {"cancel_requested": True, "benchmark_id": benchmark.id}
