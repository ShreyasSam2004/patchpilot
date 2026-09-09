"""Event bus tests.

The bus has one hard guarantee: **a failing sink must never break a run.**
Persistence problems should degrade observability, not abort an in-flight
agent. That guarantee is only as good as the error handler, so the handler
itself is tested here.
"""

from __future__ import annotations

import asyncio

import pytest

from patchpilot.domain.enums import EventType, RunState
from patchpilot.events.bus import Event, EventBus, LoggingSink, MemorySink, RunEmitter


class ExplodingSink:
    """A sink that always raises, like a full disk or a closed connection."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, event: Event) -> None:
        self.calls += 1
        raise RuntimeError("sink is on fire")


async def test_a_failing_sink_does_not_break_the_run() -> None:
    exploding = ExplodingSink()
    memory = MemorySink()
    bus = EventBus("run-1", [exploding, memory])

    await bus.emit(EventType.RUN_STARTED, {"repository": "demo"})
    await bus.emit(EventType.STATE_CHANGED, {"state": "TESTING"})

    assert exploding.calls == 2, "the failing sink was still called"
    assert len(memory.events) == 2, "later sinks still received the event"


async def test_the_failure_handler_survives_every_event_type() -> None:
    """Regression: the handler used structlog's reserved `event` kwarg.

    That raised TypeError from inside the except block, so a sink failure
    escaped and killed the run — exactly what the handler exists to prevent.
    """
    bus = EventBus("run-2", [ExplodingSink(), LoggingSink()])
    for event_type in EventType:
        await bus.emit(event_type, {"state": "TESTING"})


async def test_sequence_numbers_are_monotonic_and_unique() -> None:
    memory = MemorySink()
    bus = EventBus("run-3", [memory])
    for _ in range(25):
        await bus.emit(EventType.TOOL_CALLED, {"tool": "read_file"})

    seqs = [e.seq for e in memory.events]
    assert seqs == list(range(1, 26))
    assert bus.last_seq == 25


async def test_concurrent_emits_do_not_collide() -> None:
    memory = MemorySink()
    bus = EventBus("run-4", [memory])
    await asyncio.gather(
        *(bus.emit(EventType.TOOL_CALLED, {"i": i}) for i in range(50))
    )
    seqs = sorted(e.seq for e in memory.events)
    assert seqs == list(range(1, 51)), "sequence numbers must be unique under concurrency"


async def test_oversized_arguments_are_trimmed() -> None:
    memory = MemorySink()
    emitter = RunEmitter(EventBus("run-5", [memory]))
    await emitter.tool_called(
        tool="apply_patch", arguments={"diff": "x" * 50_000}, call_id="c1"
    )
    stored = memory.events[0].payload["arguments"]["diff"]
    assert len(stored) < 20_000
    assert "more chars" in stored


async def test_emitter_records_the_current_iteration() -> None:
    memory = MemorySink()
    emitter = RunEmitter(EventBus("run-6", [memory]))
    await emitter.iteration_started(3, {"iterations": 3})
    await emitter.agent_message("looking at the token module")

    assert all(e.iteration == 3 for e in memory.events)
    assert memory.events[-1].payload["text"] == "looking at the token module"


async def test_empty_agent_messages_are_not_stored() -> None:
    memory = MemorySink()
    emitter = RunEmitter(EventBus("run-7", [memory]))
    await emitter.agent_message("   ")
    assert memory.events == []


async def test_state_transitions_carry_the_previous_state() -> None:
    memory = MemorySink()
    emitter = RunEmitter(EventBus("run-8", [memory]))
    await emitter.state_changed(RunState.TESTING, RunState.PATCHING)

    payload = memory.events[0].payload
    assert payload["state"] == "TESTING"
    assert payload["previous"] == "PATCHING"


def test_events_serialise_for_the_api() -> None:
    event = Event(type=EventType.TEST_FINISHED, payload={"passed": 10}, seq=4, run_id="r")
    data = event.to_dict()
    assert data["type"] == "TEST_FINISHED"
    assert data["seq"] == 4
    assert data["payload"]["passed"] == 10
    assert data["created_at"].endswith("+00:00")


@pytest.mark.parametrize("event_type", list(EventType))
def test_every_event_type_is_an_upper_snake_string(event_type: EventType) -> None:
    """The frontend switches on these values, so they are a stable contract."""
    assert str(event_type) == event_type.value == event_type.name
    assert event_type.value.isupper()
