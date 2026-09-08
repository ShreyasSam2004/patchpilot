"""Scripted provider — a **test double**, not a product feature.

It lets the agent loop, anti-loop logic, event stream, persistence and API be
tested deterministically and for free. It is selected only by
``PATCHPILOT_PROVIDER=scripted`` and the UI labels any run that used it, so a
scripted run can never be mistaken for a real model result or contribute to a
benchmark number.

Two modes:

* **script** — a fixed list of :class:`ScriptedTurn` played in order;
* **responder** — a callable that sees the transcript and decides, which is how
  the tests simulate "first patch fails, agent reads the failure, second patch
  passes".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .base import Message, ModelProvider, ModelResponse, ToolCall, Usage


@dataclass
class ScriptedTurn:
    """One canned model turn."""

    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def to_response(self, model: str, index: int) -> ModelResponse:
        return ModelResponse(
            text=self.text,
            tool_calls=[
                ToolCall(id=f"call_{index}_{i}", name=name, arguments=args)
                for i, (name, args) in enumerate(self.tool_calls)
            ],
            usage=Usage(input_tokens=100, output_tokens=50, cost_usd=0.0),
            stop_reason="tool_use" if self.tool_calls else "end_turn",
            model=model,
        )


Responder = Callable[[list[Message], int], ScriptedTurn]


class ScriptedProvider(ModelProvider):
    """Deterministic provider for tests."""

    name = "scripted"

    def __init__(
        self,
        model: str = "scripted-model",
        script: list[ScriptedTurn] | None = None,
        responder: Responder | None = None,
    ) -> None:
        super().__init__(model)
        if script is None and responder is None:
            raise ValueError("ScriptedProvider needs either a script or a responder")
        self._script = list(script or [])
        self._responder = responder
        self.calls = 0
        self.seen_systems: list[str] = []
        self.seen_tools: list[list[str]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ModelResponse:
        started = time.perf_counter()
        index = self.calls
        self.calls += 1
        self.seen_systems.append(system)
        self.seen_tools.append([tool["name"] for tool in (tools or [])])

        if self._responder is not None:
            turn = self._responder(messages, index)
        elif index < len(self._script):
            turn = self._script[index]
        else:
            # Running off the end of a script means the loop iterated more than
            # the test expected; finishing honestly is more useful than hanging.
            turn = ScriptedTurn(
                text="Script exhausted.",
                tool_calls=[
                    ("finish", {"resolved": False, "summary": "scripted provider exhausted"})
                ],
            )

        response = turn.to_response(self.model, index)
        response.latency_ms = int((time.perf_counter() - started) * 1000)
        self._record(response.usage)
        return response

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "is_test_double": True}


def last_tool_output(messages: list[Message], tool: str | None = None) -> str:
    """Helper for responders: the most recent tool result, optionally by name."""
    for message in reversed(messages):
        if message.role != "tool":
            continue
        for result in reversed(message.tool_results):
            if tool is None or result.name == tool:
                return result.content
    return ""
