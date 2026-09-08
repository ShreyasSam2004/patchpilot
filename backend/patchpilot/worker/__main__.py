"""Standalone worker process: ``python -m patchpilot.worker``."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from ..config import get_settings
from ..db.session import dispose_db, init_db
from ..logging_setup import configure_logging, get_logger
from .runner import Worker

log = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()
    await init_db(settings)

    worker = Worker(settings)
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _request_stop() -> None:
        log.info("worker.signal_received")
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _request_stop)

    worker_task = asyncio.create_task(worker.start())
    try:
        await stopping.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await worker.stop(drain=True)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await dispose_db()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
