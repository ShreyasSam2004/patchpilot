"""Database-backed job queue.

ADR-0002: no Celery, no Redis. A run row *is* the job. A worker claims one with
a conditional UPDATE:

    UPDATE runs SET state='PREPARING', worker_id=:me
    WHERE id=:id AND state='PENDING'

Whoever's UPDATE reports ``rowcount == 1`` owns the job. This is atomic on both
SQLite and PostgreSQL without advisory locks or ``SKIP LOCKED``, and it means
the whole system runs from one ``pip install`` with no broker to operate.

The cost is polling latency (default 0.5 s), which is irrelevant next to a
multi-minute agent run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import BenchmarkRun, Run
from ..domain.enums import BenchmarkRunStatus, RunState
from ..logging_setup import get_logger

log = get_logger(__name__)

#: A claimed run whose heartbeat is older than this is considered abandoned
#: (worker crashed or was killed) and is requeued.
STALE_AFTER = timedelta(minutes=15)


async def claim_next_run(session: AsyncSession, worker_id: str) -> Run | None:
    """Atomically claim one queued run, or return ``None``."""
    candidates = (
        await session.execute(
            sa.select(Run.id)
            .where(Run.state == str(RunState.PENDING))
            .order_by(Run.queued_at)
            .limit(5)
        )
    ).scalars().all()

    now = datetime.now(UTC)
    for run_id in candidates:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                sa.update(Run)
                .where(Run.id == run_id, Run.state == str(RunState.PENDING))
                .values(
                    state=str(RunState.PREPARING),
                    worker_id=worker_id,
                    started_at=now,
                    heartbeat_at=now,
                )
            ),
        )
        if result.rowcount == 1:
            await session.commit()
            run = await session.get(Run, run_id)
            log.info("queue.claimed", run_id=run_id, worker=worker_id)
            return run
        await session.rollback()
    return None


async def claim_next_benchmark(session: AsyncSession, worker_id: str) -> BenchmarkRun | None:
    """Atomically claim one queued benchmark batch."""
    candidates = (
        await session.execute(
            sa.select(BenchmarkRun.id)
            .where(BenchmarkRun.status == str(BenchmarkRunStatus.PENDING))
            .order_by(BenchmarkRun.created_at)
            .limit(5)
        )
    ).scalars().all()

    for benchmark_id in candidates:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                sa.update(BenchmarkRun)
                .where(
                    BenchmarkRun.id == benchmark_id,
                    BenchmarkRun.status == str(BenchmarkRunStatus.PENDING),
                )
                .values(
                    status=str(BenchmarkRunStatus.GENERATING),
                    started_at=datetime.now(UTC),
                )
            ),
        )
        if result.rowcount == 1:
            await session.commit()
            log.info("queue.claimed_benchmark", benchmark_id=benchmark_id, worker=worker_id)
            return await session.get(BenchmarkRun, benchmark_id)
        await session.rollback()
    return None


async def heartbeat(session: AsyncSession, run_id: str) -> bool:
    """Refresh liveness and report whether a cancel has been requested."""
    result = await session.execute(
        sa.select(Run.cancel_requested).where(Run.id == run_id)
    )
    cancel = bool(result.scalar_one_or_none())
    await session.execute(
        sa.update(Run).where(Run.id == run_id).values(heartbeat_at=datetime.now(UTC))
    )
    return cancel


async def requeue_stale_runs(session: AsyncSession) -> int:
    """Return abandoned runs to the queue so a crash is not a lost job."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    active = [
        str(RunState.PREPARING),
        str(RunState.ANALYZING),
        str(RunState.SEARCHING),
        str(RunState.READING),
        str(RunState.HYPOTHESIZING),
        str(RunState.PATCHING),
        str(RunState.TESTING),
        str(RunState.REFLECTING),
    ]
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            sa.update(Run)
            .where(
                Run.state.in_(active),
                sa.or_(Run.heartbeat_at.is_(None), Run.heartbeat_at < cutoff),
            )
            .values(
                state=str(RunState.PENDING),
                worker_id=None,
                started_at=None,
                heartbeat_at=None,
            )
        ),
    )
    count = int(result.rowcount or 0)
    if count:
        log.warning("queue.requeued_stale", count=count)
    return count


async def queue_depth(session: AsyncSession) -> int:
    result = await session.execute(
        sa.select(sa.func.count()).select_from(Run).where(Run.state == str(RunState.PENDING))
    )
    return int(result.scalar_one())
