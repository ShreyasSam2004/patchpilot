"""Database event sink.

Persists every event as it happens, and projects the interesting ones into the
denormalised tables the UI reads (tool calls, hypotheses, patches, test runs).
Writing both is deliberate: the event log is the append-only truth, while the
projections make "show me every test run for this run" a single indexed query
instead of a JSON scan.

Persistence failures are logged and swallowed by :class:`EventBus`, so a full
disk degrades observability without killing an in-flight run.
"""

from __future__ import annotations

from typing import Any

from ..db.models import Event as EventRow
from ..db.models import HypothesisRow, PatchRow, Run, TestRunRow, ToolCallRow
from ..db.session import session_scope
from ..domain.enums import EventType
from ..logging_setup import get_logger
from .bus import Event

log = get_logger(__name__)


class DatabaseSink:
    """Writes events and their projections to the database.

    A tool call arrives as two events — ``TOOL_CALLED`` carries the model's
    arguments, ``TOOL_RESULT`` carries the outcome — but the UI wants one row.
    The arguments are held here for the moment between the two rather than
    joining across the event log at read time.
    """

    def __init__(self, run_id: str, settings: Any = None) -> None:
        self.run_id = run_id
        self._settings = settings
        self._pending_arguments: dict[str, dict[str, Any]] = {}

    async def handle(self, event: Event) -> None:
        if event.type is EventType.TOOL_CALLED:
            call_id = event.payload.get("call_id")
            if call_id:
                self._pending_arguments[call_id] = event.payload.get("arguments", {}) or {}

        async with session_scope(self._settings) as session:
            session.add(
                EventRow(
                    run_id=self.run_id,
                    seq=event.seq,
                    type=str(event.type),
                    iteration=event.iteration,
                    payload=event.payload,
                    created_at=event.created_at,
                )
            )
            self._project(session, event)
            await session.flush()

            run = await session.get(Run, self.run_id)
            if run is not None:
                run.last_seq = event.seq
                if event.type is EventType.STATE_CHANGED:
                    run.state = event.payload.get("state", run.state)

    def _project(self, session: Any, event: Event) -> None:
        payload = event.payload

        if event.type is EventType.TOOL_RESULT:
            call_id = payload.get("call_id", "")
            session.add(
                ToolCallRow(
                    run_id=self.run_id,
                    iteration=event.iteration,
                    tool=payload.get("tool", ""),
                    arguments=self._pending_arguments.pop(call_id, {}),
                    ok=bool(payload.get("ok", True)),
                    summary=str(payload.get("summary", ""))[:2000],
                    error_code=payload.get("error_code"),
                    duration_ms=int(payload.get("duration_ms", 0)),
                )
            )

        elif event.type is EventType.HYPOTHESIS_CREATED:
            session.add(
                HypothesisRow(
                    run_id=self.run_id,
                    iteration=event.iteration,
                    index=int(payload.get("index", 1)),
                    suspected_cause=payload.get("cause", ""),
                    evidence=payload.get("evidence", ""),
                    files_involved=payload.get("files", []) or [],
                    proposed_fix=payload.get("fix", ""),
                    validation_plan=payload.get("validation", ""),
                    supersedes_previous=bool(payload.get("supersedes", False)),
                )
            )

        elif event.type is EventType.PATCH_APPLIED:
            session.add(
                PatchRow(
                    run_id=self.run_id,
                    iteration=event.iteration,
                    diff=payload.get("diff", ""),
                    files=payload.get("files", []) or [],
                    insertions=int(payload.get("insertions", 0)),
                    deletions=int(payload.get("deletions", 0)),
                    applied=True,
                )
            )

        elif event.type is EventType.TEST_FINISHED:
            session.add(
                TestRunRow(
                    run_id=self.run_id,
                    iteration=event.iteration,
                    command=payload.get("command", ""),
                    framework=payload.get("framework", "unknown"),
                    passed=int(payload.get("passed", 0)),
                    failed=int(payload.get("failed", 0)),
                    errors=int(payload.get("errors", 0)),
                    skipped=int(payload.get("skipped", 0)),
                    total=int(payload.get("total", 0)),
                    green=bool(payload.get("green", False)),
                    timed_out=bool(payload.get("timed_out", False)),
                    duration_ms=int(payload.get("duration_ms", 0)),
                    failing_tests=payload.get("failing_tests", []) or [],
                    output=payload.get("output", ""),
                )
            )
