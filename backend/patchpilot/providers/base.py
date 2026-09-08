"""Model provider interface.

The agent talks to a :class:`ModelProvider`, never to a vendor SDK. A provider
takes a conversation plus tool schemas and returns text and/or tool calls in a
normalised shape.

Deliberate constraint: the conversation carries only messages we are willing to
persist and show. Providers must not surface hidden reasoning to the agent, and
the agent never writes reasoning into the record — only the assistant's short
visible summary, tool calls, and tool results.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolCallResult:
    """The observation returned for a :class:`ToolCall`."""

    id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Message:
    """One turn of the conversation."""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolCallResult] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.content and not self.tool_calls and not self.tool_results


@dataclass(slots=True)
class Usage:
    """Token accounting for one call, plus a cost estimate when known."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=(
                None
                if self.cost_usd is None and other.cost_usd is None
                else (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
            ),
        )


@dataclass(slots=True)
class ModelResponse:
    """Normalised result of one model call."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    model: str = ""
    latency_ms: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ModelProvider(ABC):
    """A model that can hold a conversation and request tool calls."""

    name: str = "abstract"

    def __init__(self, model: str, **_: Any) -> None:
        self.model = model
        self._usage = Usage()

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ModelResponse:
        """One turn. Implementations must not raise for ordinary tool-use stops."""

    def usage(self) -> Usage:
        """Cumulative usage across every call made by this provider instance."""
        return self._usage

    def _record(self, usage: Usage) -> None:
        self._usage = self._usage + usage

    async def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any client resources. Providers without one need not override."""

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}


#: Published per-million-token prices, used only for an *estimate* that is
#: labelled as such in the UI. Unknown models yield ``cost_usd=None`` rather
#: than a fabricated number.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # anthropic
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # openai
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o4-mini": (1.1, 4.4),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimate USD cost, or ``None`` when the model's pricing is unknown."""
    key = model.lower()
    prices = PRICING_USD_PER_MTOK.get(key)
    if prices is None:
        for known, value in PRICING_USD_PER_MTOK.items():
            if key.startswith(known):
                prices = value
                break
    if prices is None:
        return None
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
