"""Structured run events.

Events are the run's observable history. They are append-only, carry a
monotonic per-run sequence number, and are the single source the UI renders
from — so a live run and a replayed finished run look identical.

What is stored: lifecycle transitions, tool calls with their arguments, tool
result *summaries*, structured hypotheses, patches, and test outcomes.

What is never stored: model chain-of-thought. The agent's visible one-line
status text is stored as ``AGENT_MESSAGE``; nothing else from the model's
internal reasoning enters the record.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ..domain.enums import EventType, RunState
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Event:
    """One thing that happened during a run."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    iteration: int = 0
    seq: int = 0
    run_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": str(self.type),
            "iteration": self.iteration,
            "payload": self.payload,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
        }


@runtime_checkable
class EventSink(Protocol):
    """Anything that can receive events."""

    async def handle(self, event: Event) -> None: ...


class MemorySink:
    """Collects events in a list. Used by tests and the CLI."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type is event_type]

    def types(self) -> list[str]:
        return [str(e.type) for e in self.events]


class LoggingSink:
    """Mirrors events to the structured log."""

    def __init__(self, level: str = "info") -> None:
        self.level = level

    async def handle(self, event: Event) -> None:
        getattr(log, self.level)(
            f"event.{event.type.lower()}",
            run_id=event.run_id,
            seq=event.seq,
            iteration=event.iteration,
            **{k: v for k, v in event.payload.items() if k in _LOGGED_KEYS},
        )


_LOGGED_KEYS = {"tool", "state", "status", "command", "passed", "failed", "summary", "limit"}


class CallbackSink:
    """Adapts a plain async callable into a sink."""

    def __init__(self, callback: Callable[[Event], Awaitable[None]]) -> None:
        self._callback = callback

    async def handle(self, event: Event) -> None:
        await self._callback(event)


class EventBus:
    """Fans one event out to every sink, isolating sink failures.

    A sink that raises must not take down the run: persistence problems are
    logged and the agent continues.
    """

    def __init__(self, run_id: str, sinks: list[EventSink] | None = None) -> None:
        self.run_id = run_id
        self.sinks: list[EventSink] = list(sinks or [])
        self._seq = 0
        self._lock = asyncio.Lock()

    def add_sink(self, sink: EventSink) -> None:
        self.sinks.append(sink)

    async def emit(
        self, event_type: EventType, payload: dict[str, Any] | None = None, iteration: int = 0
    ) -> Event:
        async with self._lock:
            self._seq += 1
            event = Event(
                type=event_type,
                payload=payload or {},
                iteration=iteration,
                seq=self._seq,
                run_id=self.run_id,
            )
        for sink in self.sinks:
            try:
                await sink.handle(event)
            except Exception as exc:
                log.warning(
                    "events.sink_failed",
                    sink=type(sink).__name__,
                    event=str(event_type),
                    error=str(exc),
                )
        return event

    @property
    def last_seq(self) -> int:
        return self._seq


class RunEmitter:
    """Typed helpers over :class:`EventBus`.

    Tools and the orchestrator call these instead of constructing payloads by
    hand, which keeps the event schema in one place.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.iteration = 0
        self._started = time.monotonic()

    def set_iteration(self, iteration: int) -> None:
        self.iteration = iteration

    async def _emit(self, event_type: EventType, **payload: Any) -> Event:
        return await self.bus.emit(event_type, payload, iteration=self.iteration)

    # -- lifecycle -----------------------------------------------------------
    async def run_started(self, **payload: Any) -> None:
        await self._emit(EventType.RUN_STARTED, **payload)

    async def repository_ready(self, **payload: Any) -> None:
        await self._emit(EventType.REPOSITORY_READY, **payload)

    async def issue_analyzed(self, **payload: Any) -> None:
        await self._emit(EventType.ISSUE_ANALYZED, **payload)

    async def state_changed(self, state: RunState, previous: RunState | None = None) -> None:
        await self._emit(
            EventType.STATE_CHANGED,
            state=str(state),
            previous=str(previous) if previous else None,
        )

    async def iteration_started(self, iteration: int, budget: dict[str, Any]) -> None:
        self.iteration = iteration
        await self._emit(EventType.ITERATION_STARTED, iteration=iteration, budget=budget)

    async def iteration_completed(self, iteration: int, **payload: Any) -> None:
        await self._emit(EventType.ITERATION_COMPLETED, iteration=iteration, **payload)

    # -- model ---------------------------------------------------------------
    async def model_call_started(self, model: str, approx_tokens: int) -> None:
        await self._emit(EventType.MODEL_CALL_STARTED, model=model, approx_tokens=approx_tokens)

    async def model_call_finished(
        self, *, model: str, latency_ms: int, usage: dict[str, Any], stop_reason: str
    ) -> None:
        await self._emit(
            EventType.MODEL_CALL_FINISHED,
            model=model,
            latency_ms=latency_ms,
            usage=usage,
            stop_reason=stop_reason,
        )

    async def agent_message(self, text: str) -> None:
        """The agent's short visible status line — never hidden reasoning."""
        if text.strip():
            await self._emit(EventType.AGENT_MESSAGE, text=text.strip()[:2000])

    # -- tools ---------------------------------------------------------------
    async def tool_called(self, *, tool: str, arguments: dict[str, Any], call_id: str) -> None:
        await self._emit(
            EventType.TOOL_CALLED, tool=tool, arguments=_safe_args(arguments), call_id=call_id
        )

    async def tool_result(
        self,
        *,
        tool: str,
        call_id: str,
        ok: bool,
        summary: str,
        duration_ms: int,
        data: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._emit(
            EventType.TOOL_RESULT,
            tool=tool,
            call_id=call_id,
            ok=ok,
            summary=summary[:500],
            duration_ms=duration_ms,
            data=_safe_args(data or {}),
            error_code=error_code,
        )

    # -- agent artefacts -----------------------------------------------------
    async def hypothesis(self, **payload: Any) -> None:
        await self._emit(EventType.HYPOTHESIS_CREATED, **payload)

    async def patch_applied(
        self, *, files: list[str], insertions: int, deletions: int, diff: str
    ) -> None:
        await self._emit(
            EventType.PATCH_APPLIED,
            files=files,
            insertions=insertions,
            deletions=deletions,
            diff=diff[:200_000],
        )

    async def test_started(self, command: str) -> None:
        await self._emit(EventType.TEST_STARTED, command=command)

    async def test_finished(
        self, *, command: str, outcome: Any, duration_ms: int, output: str
    ) -> None:
        await self._emit(
            EventType.TEST_FINISHED,
            command=command,
            passed=outcome.passed,
            failed=outcome.failed,
            errors=outcome.errors,
            skipped=outcome.skipped,
            total=outcome.total,
            green=outcome.green,
            framework=outcome.framework,
            timed_out=outcome.timed_out,
            failing_tests=outcome.failing_tests[:20],
            duration_ms=duration_ms,
            output=output[:60_000],
        )

    # -- control -------------------------------------------------------------
    async def loop_detected(self, *, kind: str, detail: str) -> None:
        await self._emit(EventType.LOOP_DETECTED, kind=kind, detail=detail)

    async def recovery_triggered(self, *, kind: str, directive: str) -> None:
        await self._emit(EventType.RECOVERY_TRIGGERED, kind=kind, directive=directive[:2000])

    async def budget_exceeded(self, *, limit: str, snapshot: dict[str, Any]) -> None:
        await self._emit(EventType.BUDGET_EXCEEDED, limit=limit, snapshot=snapshot)

    async def warning(self, message: str, **payload: Any) -> None:
        await self._emit(EventType.WARNING, message=message, **payload)

    async def run_resolved(self, **payload: Any) -> None:
        await self._emit(EventType.RUN_RESOLVED, **payload)

    async def run_failed(self, **payload: Any) -> None:
        await self._emit(EventType.RUN_FAILED, **payload)

    async def run_cancelled(self, **payload: Any) -> None:
        await self._emit(EventType.RUN_CANCELLED, **payload)


_MAX_ARG_CHARS = 8000


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Trim oversized argument values so an event row stays a reasonable size."""
    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
            out[key] = value[:_MAX_ARG_CHARS] + f"...[{len(value) - _MAX_ARG_CHARS} more chars]"
        else:
            out[key] = value
    return out
