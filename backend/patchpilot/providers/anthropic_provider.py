"""Anthropic provider.

Maps PatchPilot's normalised :class:`Message` list onto the Messages API and
back. Notes on the mapping:

* tool results must be ``user`` messages containing ``tool_result`` blocks, and
  every ``tool_use`` id must be answered — the orchestrator guarantees this;
* the system prompt is cached (``cache_control``) because it carries the tool
  catalogue and the issue text and is identical on every turn of a run;
* ``thinking`` blocks, if a model emits them, are dropped rather than stored.
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import ConfigurationError, ProviderError
from ..logging_setup import get_logger
from .base import Message, ModelProvider, ModelResponse, ToolCall, Usage, estimate_cost

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 4,
        timeout_s: float = 300.0,
        effort: str | None = "high",
    ) -> None:
        super().__init__(model)
        self.effort = effort
        if not api_key:
            raise ConfigurationError(
                "Anthropic provider requires an API key. Set PATCHPILOT_ANTHROPIC_API_KEY "
                "or ANTHROPIC_API_KEY."
            )
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConfigurationError(
                "the 'anthropic' package is not installed; pip install 'patchpilot[anthropic]'"
            ) from exc

        kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": max_retries,
                                  "timeout": timeout_s}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic_messages(messages),
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
        }

        # Sampling parameters (temperature / top_p / top_k) were removed from the
        # Messages API on the current Claude models — passing one is a 400, and
        # anthropic>=1.x does not even accept the keyword. Reasoning depth and
        # token spend are controlled by `output_config.effort` instead.
        # `temperature` stays in the ModelProvider signature because OpenAI
        # still honours it; here it is deliberately ignored.
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
        if tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                }
                for tool in tools
            ]

        started = time.perf_counter()
        try:
            response = await self._client.messages.create(**payload)
        except Exception as exc:
            raise ProviderError(f"Anthropic request failed: {type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if isinstance(block.input, dict) else {},
                    )
                )
            # 'thinking' / 'redacted_thinking' blocks are intentionally discarded.

        raw_usage = response.usage
        usage = Usage(
            input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
        )
        usage.cost_usd = estimate_cost(self.model, usage.input_tokens, usage.output_tokens)
        self._record(usage)

        return ModelResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=response.stop_reason or "",
            model=response.model,
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        await self._client.close()


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate the normalised transcript into Anthropic's block format."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue  # carried separately in the `system` parameter

        if message.role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": result.id,
                    "content": result.content or "(no output)",
                    **({"is_error": True} if result.is_error else {}),
                }
                for result in message.tool_results
            ]
            if blocks:
                out.append({"role": "user", "content": blocks})
            continue

        if message.role == "assistant":
            blocks = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        if message.content:
            out.append({"role": "user", "content": [{"type": "text", "text": message.content}]})
    return out
