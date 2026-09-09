"""SWE-bench batch runner.

For each instance: prepare the repository at ``base_commit``, hand the problem
statement to the same agent the UI uses, capture ``git diff`` as the model
patch. Then run the official evaluator once over the whole batch and record
what it says.

Two verdicts are kept apart throughout:

* ``agent_status``     — what the agent concluded about its own tests;
* ``benchmark_status`` — what the official harness concluded.

Only ``benchmark_status`` is ever aggregated into a resolve rate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from ..agent.orchestrator import AgentTask
from ..agent.session import SessionConfig, agent_session
from ..config import Settings, get_settings
from ..db.models import BenchmarkRun, BenchmarkTask, Run
from ..db.session import session_scope
from ..domain.enums import BenchmarkRunStatus, BenchmarkStatus, RunState
from ..events.recorder import DatabaseSink
from ..ids import new_id, short_code
from ..logging_setup import get_logger
from ..runs import service as run_service
from .dataset import SweBenchInstance, load_instances
from .evaluator import (
    Prediction,
    evaluator_preflight,
    run_official_evaluation,
    write_predictions,
)
from .images import instance_image_name

log = get_logger(__name__)


def build_agent_task(instance: SweBenchInstance, run_id: str) -> AgentTask:
    """Turn a SWE-bench instance into an agent task.

    The agent is given the problem statement and the repository only. It is
    *not* given FAIL_TO_PASS, PASS_TO_PASS or the gold patch — those are the
    answer key, and leaking them would make any resulting number meaningless.
    """
    return AgentTask(
        run_id=run_id,
        issue=instance.problem_statement,
        repository=instance.repo,
        repo_url=instance.repo_url,
        base_commit=instance.base_commit,
        test_command=instance.suggested_test_command(),
        hints=None,
    )


async def create_benchmark(
    session,
    *,
    name: str | None = None,
    dataset: str | None = None,
    split: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    instance_ids: list[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    max_iterations: int | None = None,
    evaluate: bool = True,
    settings: Settings | None = None,
) -> BenchmarkRun:
    """Queue a benchmark batch. Instances are resolved now so the row is complete."""
    settings = settings or get_settings()
    dataset = dataset or settings.swebench_dataset
    split = split or settings.swebench_split

    instances = load_instances(
        dataset, split, instance_ids=instance_ids, limit=limit, offset=offset
    )
    if not instances:
        from ..errors import BenchmarkError

        raise BenchmarkError("no instances selected")

    run_key = f"patchpilot-{short_code(6).lower()}"
    benchmark = BenchmarkRun(
        id=new_id(),
        name=name or f"{dataset.split('/')[-1]} x{len(instances)}",
        dataset=dataset,
        split=split,
        run_key=run_key,
        provider=provider or settings.provider,
        model=model or settings.model,
        config={
            "provider": provider or settings.provider,
            "model": model or settings.model,
            "sandbox": sandbox or settings.sandbox,
            "max_iterations": max_iterations or settings.max_iterations,
            "max_tool_calls": settings.max_tool_calls,
            "max_runtime_s": settings.max_runtime_s,
            "max_cost_usd": settings.max_cost_usd,
            "evaluate": evaluate,
            "limit": limit,
            "offset": offset,
            "instance_ids": instance_ids,
            # Recorded so the batch can be reproduced exactly from this row.
            "reproduce": {
                "dataset": dataset,
                "split": split,
                "selection": (
                    "explicit"
                    if instance_ids
                    else f"sorted[{offset}:{offset + (limit or 0)}]"
                ),
            },
        },
        instance_ids=[i.instance_id for i in instances],
        total_tasks=len(instances),
        status=str(BenchmarkRunStatus.PENDING),
    )
    session.add(benchmark)
    await session.flush()

    for instance in instances:
        session.add(
            BenchmarkTask(
                id=new_id(),
                benchmark_id=benchmark.id,
                instance_id=instance.instance_id,
                repo=instance.repo,
                base_commit=instance.base_commit,
                problem_statement=instance.problem_statement,
                environment_setup_commit=instance.environment_setup_commit,
                agent_status=str(RunState.PENDING),
                benchmark_status=str(BenchmarkStatus.PENDING),
            )
        )
    await session.flush()

    log.info(
        "benchmark.created",
        benchmark_id=benchmark.id,
        run_key=run_key,
        instances=len(instances),
        evaluate=evaluate,
    )
    return benchmark


async def execute_benchmark(benchmark_id: str, settings: Settings | None = None) -> None:
    """Generate patches for every task, then evaluate the batch officially."""
    settings = settings or get_settings()

    async with session_scope(settings) as session:
        benchmark = await session.get(BenchmarkRun, benchmark_id)
        if benchmark is None:
            log.warning("benchmark.missing", benchmark_id=benchmark_id)
            return
        config = dict(benchmark.config or {})
        dataset, split = benchmark.dataset, benchmark.split
        run_key = benchmark.run_key
        model_name = benchmark.model
        instance_ids = list(benchmark.instance_ids or [])
        benchmark.status = str(BenchmarkRunStatus.GENERATING)
        benchmark.started_at = benchmark.started_at or datetime.now(UTC)

    try:
        instances = {
            i.instance_id: i
            for i in load_instances(dataset, split, instance_ids=instance_ids)
        }

        for instance_id in instance_ids:
            async with session_scope(settings) as session:
                current = await session.get(BenchmarkRun, benchmark_id)
                if current is not None and current.cancel_requested:
                    log.info("benchmark.cancelled", benchmark_id=benchmark_id)
                    await _finish(session, benchmark_id, BenchmarkRunStatus.CANCELLED)
                    return

            await _run_one_task(benchmark_id, instances[instance_id], config, settings)

        if config.get("evaluate", True):
            await _evaluate_batch(
                benchmark_id, run_key, dataset, split, model_name, settings
            )
        else:
            async with session_scope(settings) as session:
                await _recompute(session, benchmark_id)
                await _finish(session, benchmark_id, BenchmarkRunStatus.COMPLETED)

    except Exception as exc:
        log.exception("benchmark.failed", benchmark_id=benchmark_id)
        async with session_scope(settings) as session:
            benchmark = await session.get(BenchmarkRun, benchmark_id)
            if benchmark is not None:
                benchmark.status = str(BenchmarkRunStatus.FAILED)
                benchmark.error = f"{type(exc).__name__}: {exc}"
                benchmark.finished_at = datetime.now(UTC)


async def _run_one_task(
    benchmark_id: str,
    instance: SweBenchInstance,
    config: dict[str, Any],
    settings: Settings,
) -> None:
    """Run the agent on one instance and store its patch."""
    async with session_scope(settings) as session:
        task = (
            await session.execute(
                sa.select(BenchmarkTask).where(
                    BenchmarkTask.benchmark_id == benchmark_id,
                    BenchmarkTask.instance_id == instance.instance_id,
                )
            )
        ).scalar_one()
        if task.benchmark_status != str(BenchmarkStatus.PENDING):
            return  # already processed (resumed batch)

        run = Run(
            id=new_id(),
            code=short_code(),
            repository=instance.repo,
            repo_url=instance.repo_url,
            base_commit=instance.base_commit,
            issue=instance.problem_statement,
            issue_title=instance.instance_id,
            test_command=instance.suggested_test_command(),
            provider=config.get("provider", settings.provider),
            model=config.get("model", settings.model),
            sandbox=config.get("sandbox", settings.sandbox),
            config=config,
            state=str(RunState.PENDING),
            benchmark_task_id=task.id,
        )
        session.add(run)
        task.run_id = run.id
        task.started_at = datetime.now(UTC)
        task.benchmark_status = str(BenchmarkStatus.RUNNING)
        await session.flush()
        run_id = run.id

    agent_task = build_agent_task(instance, run_id)
    if config.get("max_iterations"):
        agent_task.budget_overrides["max_iterations"] = config["max_iterations"]

    sandbox_kind = config.get("sandbox") or settings.sandbox
    # In the docker sandbox, run inside the official per-instance image: it has
    # the repository's dependencies installed, so `run_tests` observes what the
    # evaluator will. A generic image cannot import numpy, scipy, Django, ...
    use_instance_image = (
        sandbox_kind == "docker" and settings.swebench_instance_images
    )
    session_config = SessionConfig(
        provider=config.get("provider"),
        model=config.get("model"),
        sandbox=config.get("sandbox"),
        docker_image=(
            instance_image_name(
                instance.instance_id,
                namespace=settings.swebench_image_namespace,
                tag=settings.swebench_image_tag,
            )
            if use_instance_image
            else None
        ),
        pull_image=use_instance_image,
        allow_sandbox_fallback=True,
    )

    log.info("benchmark.task_started", instance=instance.instance_id, run_id=run_id)
    try:
        async with agent_session(
            agent_task,
            settings=settings,
            config=session_config,
            sinks=[DatabaseSink(run_id, settings)],
        ) as orchestrator:
            result = await orchestrator.run()

        async with session_scope(settings) as session:
            await run_service.apply_result(session, run_id, result)
            task = (
                await session.execute(
                    sa.select(BenchmarkTask).where(BenchmarkTask.run_id == run_id)
                )
            ).scalar_one()
            task.agent_status = str(result.state)
            task.model_patch = result.final_patch
            task.metrics = result.metrics.to_dict()
            task.finished_at = datetime.now(UTC)
            # NOT_EVALUATED, not UNRESOLVED: the evaluator has not spoken yet.
            task.benchmark_status = str(
                BenchmarkStatus.EMPTY_PATCH
                if not result.final_patch.strip()
                else BenchmarkStatus.NOT_EVALUATED
            )
            await _recompute(session, benchmark_id)

        log.info(
            "benchmark.task_finished",
            instance=instance.instance_id,
            agent_state=str(result.state),
            patch_bytes=len(result.final_patch),
        )
    except Exception as exc:
        log.exception("benchmark.task_failed", instance=instance.instance_id)
        async with session_scope(settings) as session:
            failed_task = (
                await session.execute(
                    sa.select(BenchmarkTask).where(BenchmarkTask.run_id == run_id)
                )
            ).scalar_one_or_none()
            if failed_task is not None:
                failed_task.agent_status = str(RunState.FAILED)
                failed_task.benchmark_status = str(BenchmarkStatus.ERROR)
                failed_task.error = f"{type(exc).__name__}: {exc}"
                failed_task.finished_at = datetime.now(UTC)
            await _recompute(session, benchmark_id)


async def _evaluate_batch(
    benchmark_id: str,
    run_key: str,
    dataset: str,
    split: str,
    model_name: str,
    settings: Settings,
) -> None:
    """Write predictions and hand the batch to the official harness."""
    async with session_scope(settings) as session:
        benchmark = await session.get(BenchmarkRun, benchmark_id)
        if benchmark is None:
            return
        benchmark.status = str(BenchmarkRunStatus.EVALUATING)
        tasks = (
            await session.execute(
                sa.select(BenchmarkTask).where(BenchmarkTask.benchmark_id == benchmark_id)
            )
        ).scalars().all()
        predictions = [
            Prediction(
                instance_id=t.instance_id,
                model_name_or_path=model_name,
                model_patch=t.model_patch or "",
            )
            for t in tasks
        ]
        instance_ids = [t.instance_id for t in tasks]

    work_dir = settings.swebench_run_root / run_key
    predictions_path = write_predictions(work_dir / "predictions.jsonl", predictions)

    preflight = await evaluator_preflight(settings)
    if not preflight["ready"]:
        log.warning("benchmark.evaluation_skipped", problems=preflight["problems"])
        async with session_scope(settings) as session:
            benchmark = await session.get(BenchmarkRun, benchmark_id)
            if benchmark is not None:
                benchmark.evaluated = False
                benchmark.evaluator_detail = {
                    "skipped": True,
                    "reason": "; ".join(preflight["problems"]),
                    "preflight": preflight,
                    "predictions_path": str(predictions_path),
                }
                benchmark.error = (
                    "Patches were generated but NOT evaluated: "
                    + "; ".join(preflight["problems"])
                )
                await _recompute(session, benchmark_id)
                await _finish(session, benchmark_id, BenchmarkRunStatus.COMPLETED)
        return

    outcome = await run_official_evaluation(
        predictions_path=predictions_path,
        run_key=run_key,
        dataset=dataset,
        split=split,
        model_name=model_name,
        settings=settings,
        instance_ids=instance_ids,
        working_dir=work_dir,
    )

    async with session_scope(settings) as session:
        benchmark = await session.get(BenchmarkRun, benchmark_id)
        if benchmark is None:
            return
        tasks = (
            await session.execute(
                sa.select(BenchmarkTask).where(BenchmarkTask.benchmark_id == benchmark_id)
            )
        ).scalars().all()

        if outcome.ok:
            for task in tasks:
                status = outcome.status_for(task.instance_id)
                task.benchmark_status = str(status)
                task.evaluator_report = outcome.per_instance.get(task.instance_id, {})
            benchmark.evaluated = True
            benchmark.error = None
        else:
            benchmark.evaluated = False
            benchmark.error = f"official evaluation did not complete: {outcome.error}"

        benchmark.evaluator_detail = {
            "ok": outcome.ok,
            "error": outcome.error,
            "summary": outcome.summary,
            "report_paths": outcome.report_paths[:50],
            "stdout_tail": outcome.stdout_tail[-4000:],
            "stderr_tail": outcome.stderr_tail[-4000:],
            "predictions_path": str(predictions_path),
            "harness_run_id": run_key,
        }
        await _recompute(session, benchmark_id)
        await _finish(session, benchmark_id, BenchmarkRunStatus.COMPLETED)


async def _recompute(session, benchmark_id: str) -> None:
    """Recompute aggregate counters from the task rows."""
    benchmark = await session.get(BenchmarkRun, benchmark_id)
    if benchmark is None:
        return
    tasks = (
        await session.execute(
            sa.select(BenchmarkTask).where(BenchmarkTask.benchmark_id == benchmark_id)
        )
    ).scalars().all()

    resolved = sum(1 for t in tasks if t.benchmark_status == str(BenchmarkStatus.RESOLVED))
    unresolved = sum(1 for t in tasks if t.benchmark_status == str(BenchmarkStatus.UNRESOLVED))
    errors = sum(1 for t in tasks if t.benchmark_status == str(BenchmarkStatus.ERROR))
    empty = sum(1 for t in tasks if t.benchmark_status == str(BenchmarkStatus.EMPTY_PATCH))
    completed = sum(
        1
        for t in tasks
        if t.benchmark_status
        not in {str(BenchmarkStatus.PENDING), str(BenchmarkStatus.RUNNING)}
    )

    benchmark.resolved_tasks = resolved
    benchmark.unresolved_tasks = unresolved
    benchmark.error_tasks = errors
    benchmark.empty_patch_tasks = empty
    benchmark.completed_tasks = completed
    benchmark.total_tasks = len(tasks)
    benchmark.aggregate = _aggregate(tasks, benchmark)


def _aggregate(tasks: list[BenchmarkTask], benchmark: BenchmarkRun) -> dict[str, Any]:
    """Per-batch statistics, computed only from real task rows."""
    metrics = [t.metrics or {} for t in tasks if t.metrics]
    durations = [
        (t.finished_at - t.started_at).total_seconds()
        for t in tasks
        if t.started_at is not None and t.finished_at is not None
    ]

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    agent_resolved = sum(1 for t in tasks if t.agent_status == str(RunState.RESOLVED))
    with_patch = sum(1 for t in tasks if (t.model_patch or "").strip())

    return {
        "total_runtime_s": round(sum(durations), 1) if durations else 0.0,
        "avg_runtime_s": mean(durations),
        "avg_iterations": mean([float(m.get("iterations", 0)) for m in metrics]),
        "avg_tool_calls": mean([float(m.get("tool_calls", 0)) for m in metrics]),
        "avg_files_modified": mean([float(m.get("files_modified", 0)) for m in metrics]),
        "avg_test_runs": mean([float(m.get("test_runs", 0)) for m in metrics]),
        "total_input_tokens": sum(int(m.get("input_tokens", 0)) for m in metrics),
        "total_output_tokens": sum(int(m.get("output_tokens", 0)) for m in metrics),
        "total_cost_usd": (
            round(sum(float(m.get("cost_usd") or 0) for m in metrics), 4)
            if any(m.get("cost_usd") for m in metrics)
            else None
        ),
        "tasks_with_patch": with_patch,
        "agent_reported_resolved": agent_resolved,
        "evaluated": benchmark.evaluated,
        # Present only when the official evaluator actually ran.
        "resolve_rate": benchmark.resolve_rate,
    }


async def _finish(session, benchmark_id: str, status: BenchmarkRunStatus) -> None:
    benchmark = await session.get(BenchmarkRun, benchmark_id)
    if benchmark is not None:
        benchmark.status = str(status)
        benchmark.finished_at = datetime.now(UTC)
        log.info(
            "benchmark.finished",
            benchmark_id=benchmark_id,
            status=str(status),
            resolved=benchmark.resolved_tasks,
            evaluated=benchmark.evaluated,
            resolve_rate=benchmark.resolve_rate,
        )


async def reevaluate(benchmark_id: str, settings: Settings | None = None) -> None:
    """Re-run only the official evaluation for an existing batch.

    Used when a batch generated patches but could not be graded — for example
    because Docker was unavailable at the time.
    """
    settings = settings or get_settings()
    async with session_scope(settings) as session:
        benchmark = await session.get(BenchmarkRun, benchmark_id)
        if benchmark is None:
            return
        run_key, dataset, split, model = (
            benchmark.run_key,
            benchmark.dataset,
            benchmark.split,
            benchmark.model,
        )
    await _evaluate_batch(benchmark_id, run_key, dataset, split, model, settings)


async def run_benchmark_now(benchmark_id: str, settings: Settings | None = None) -> None:
    """Execute a batch inline (CLI path) instead of waiting for a worker."""
    await execute_benchmark(benchmark_id, settings)


__all__ = [
    "build_agent_task",
    "create_benchmark",
    "execute_benchmark",
    "reevaluate",
    "run_benchmark_now",
]
