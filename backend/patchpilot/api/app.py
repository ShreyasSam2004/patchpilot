"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Settings, get_settings
from ..db.session import dispose_db, init_db
from ..errors import BenchmarkError, ConfigurationError, PatchPilotError, WorkspaceError
from ..logging_setup import configure_logging, get_logger
from ..worker.runner import Worker
from .routers import benchmarks, mcp, runs, system

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    settings.ensure_dirs()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await init_db(settings)
        worker_task: asyncio.Task | None = None
        worker: Worker | None = None

        if settings.worker_enabled:
            # Convenience for local development: one process serves the API and
            # executes runs. In production run `python -m patchpilot.worker`
            # separately and set PATCHPILOT_WORKER_ENABLED=false.
            worker = Worker(settings)
            worker_task = asyncio.create_task(worker.start())
            log.info("api.worker_embedded", concurrency=settings.worker_concurrency)

        log.info("api.ready", version=__version__, environment=settings.environment)
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop(drain=False)
            if worker_task is not None:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            await dispose_db()

    app = FastAPI(
        title="PatchPilot",
        description=(
            "Autonomous software-engineering agent: issue in, validated patch out. "
            "Benchmarked with the official SWE-bench Lite evaluator."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system.router)
    app.include_router(runs.router)
    app.include_router(benchmarks.router)
    app.include_router(mcp.router)

    @app.exception_handler(PatchPilotError)
    async def _domain_error(_: Request, exc: PatchPilotError) -> JSONResponse:
        client_errors = (ConfigurationError, BenchmarkError, WorkspaceError)
        status_code = 400 if isinstance(exc, client_errors) else 500
        log.warning("api.domain_error", error=str(exc), type=type(exc).__name__)
        return JSONResponse(
            status_code=status_code, content={"detail": str(exc), "type": type(exc).__name__}
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": "PatchPilot",
            "version": __version__,
            "docs": "/api/docs",
            "status": "/api/status",
        }

    return app


app = create_app()
