"""Background worker.

Polls the queue, executes agent runs and benchmark batches, and persists
results. Runs either inside the API process (convenient for local development)
or as a separate process (``python -m patchpilot.worker``) — the queue is in
the database, so both work and can coexist.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

from ..agent.orchestrator import AgentTask
from ..agent.session import SessionConfig, agent_session
from ..config import Settings, get_settings
from ..db.models import Run
from ..db.session import session_scope
from ..domain.enums import RunState
from ..events.recorder import DatabaseSink
from ..logging_setup import get_logger
from ..runs import service as run_service
from .queue import claim_next_benchmark, claim_next_run, heartbeat, requeue_stale_runs

log = get_logger(__name__)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    """Polls for work and executes it."""

    def __init__(self, settings: Settings | None = None, *, concurrency: int | None = None) -> None:
        self.settings = settings or get_settings()
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.worker_id = worker_identity()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._slots: asyncio.Semaphore | None = None

    async def start(self) -> None:
        self._slots = asyncio.Semaphore(self.concurrency)
        log.info("worker.started", worker=self.worker_id, concurrency=self.concurrency)

        async with session_scope(self.settings) as session:
            await requeue_stale_runs(session)

        while not self._stop.is_set():
            try:
                claimed = await self._poll_once()
            except Exception as exc:
                log.exception("worker.poll_failed", error=str(exc))
                claimed = False
            if not claimed:
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.worker_poll_interval_s
                    )

    async def _poll_once(self) -> bool:
        assert self._slots is not None
        if self._slots.locked():
            return False

        async with session_scope(self.settings) as session:
            run = await claim_next_run(session, self.worker_id)
            benchmark = None if run else await claim_next_benchmark(session, self.worker_id)

        if run is not None:
            self._spawn(self._execute_run(run.id))
            return True
        if benchmark is not None:
            self._spawn(self._execute_benchmark(benchmark.id))
            return True
        return False

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self, *, drain: bool = True) -> None:
        self._stop.set()
        if drain and self._tasks:
            log.info("worker.draining", jobs=len(self._tasks))
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        else:
            for task in list(self._tasks):
                task.cancel()
        log.info("worker.stopped", worker=self.worker_id)

    # ------------------------------------------------------------- execution
    async def _execute_run(self, run_id: str) -> None:
        assert self._slots is not None
        async with self._slots:
            await execute_run(run_id, self.settings)

    async def _execute_benchmark(self, benchmark_id: str) -> None:
        assert self._slots is not None
        async with self._slots:
            from ..bench.runner import execute_benchmark

            await execute_benchmark(benchmark_id, self.settings)


async def execute_run(run_id: str, settings: Settings | None = None) -> None:
    """Execute one queued run and persist everything it produced."""
    settings = settings or get_settings()

    async with session_scope(settings) as session:
        run = await session.get(Run, run_id)
        if run is None:
            log.warning("worker.run_missing", run_id=run_id)
            return
        task = _to_agent_task(run)
        config = SessionConfig(
            provider=run.provider,
            model=run.model,
            sandbox=run.sandbox,
            allow_sandbox_fallback=True,
        )

    sink = DatabaseSink(run_id, settings)
    heartbeat_task: asyncio.Task | None = None

    try:
        async with agent_session(
            task, settings=settings, config=config, sinks=[sink]
        ) as orchestrator:
            heartbeat_task = asyncio.create_task(_heartbeat_loop(run_id, orchestrator, settings))
            result = await orchestrator.run()

        async with session_scope(settings) as session:
            await run_service.apply_result(session, run_id, result)
        log.info(
            "worker.run_finished",
            run_id=run_id,
            state=str(result.state),
            iterations=result.metrics.iterations,
            patch_bytes=len(result.final_patch),
        )
    except Exception as exc:
        log.exception("worker.run_failed", run_id=run_id)
        async with session_scope(settings) as session:
            failed = await session.get(Run, run_id)
            if failed is not None and not RunState(failed.state).is_terminal:
                failed.state = str(RunState.FAILED)
                failed.error = f"{type(exc).__name__}: {exc}"
                failed.stop_reason = "worker error"
                failed.finished_at = datetime.now(UTC)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task


async def _heartbeat_loop(run_id: str, orchestrator, settings: Settings) -> None:
    """Keep the run marked alive and honour cancellation requests."""
    while True:
        await asyncio.sleep(10)
        try:
            async with session_scope(settings) as session:
                if await heartbeat(session, run_id):
                    log.info("worker.cancel_requested", run_id=run_id)
                    orchestrator.cancel()
                    return
        except Exception as exc:
            log.warning("worker.heartbeat_failed", run_id=run_id, error=str(exc))


def _to_agent_task(run: Run) -> AgentTask:
    overrides = (run.config or {}).get("budget_overrides") or {}
    return AgentTask(
        run_id=run.id,
        issue=run.issue,
        repository=run.repository,
        repo_url=run.repo_url,
        local_path=Path(run.local_path) if run.local_path else None,
        base_commit=run.base_commit,
        test_command=run.test_command,
        budget_overrides=overrides,
    )
