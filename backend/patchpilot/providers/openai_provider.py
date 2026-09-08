"""OpenAI provider (Chat Completions with function tools).

The normalised transcript maps onto OpenAI's format with two differences from
Anthropic worth noting:

* tool results are their own ``role="tool"`` messages keyed by
  ``tool_call_id``, one per call, rather than blocks inside a user message;
* arguments arrive as a JSON *string*, so malformed JSON is a real possibility
  and is turned into an empty-argument call the tool layer will reject with a
  helpful schema error rather than crashing the run.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..errors import ConfigurationError, ProviderError
from ..logging_setup import get_logger
from .base import Message, ModelProvider, ModelResponse, ToolCall, Usage, estimate_cost

log = get_logger(__name__)


class OpenAIProvider(ModelProvider):
    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 4,
        timeout_s: float = 300.0,
    ) -> None:
        super().__init__(model)
        if not api_key:
            raise ConfigurationError(
                "OpenAI provider requires an API key. Set PATCHPILOT_OPENAI_API_KEY "
                "or OPENAI_API_KEY."
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConfigurationError(
                "the 'openai' package is not installed; pip install 'patchpilot[openai]'"
            ) from exc

        kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": max_retries,
                                  "timeout": timeout_s}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)

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
            "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *_to_openai_messages(messages)],
        }
        if temperature:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            raise ProviderError(f"OpenAI request failed: {type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        choice = response.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            raw = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw)
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                log.warning("openai.bad_tool_arguments", tool=call.function.name)
                arguments = {}
            tool_calls.append(
                ToolCall(id=call.id, name=call.function.name, arguments=arguments)
            )

        raw_usage = response.usage
        usage = Usage(
            input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
        )
        details = getattr(raw_usage, "prompt_tokens_details", None)
        if details is not None:
            usage.cache_read_tokens = getattr(details, "cached_tokens", 0) or 0
        usage.cost_usd = estimate_cost(self.model, usage.input_tokens, usage.output_tokens)
        self._record(usage)

        return ModelResponse(
            text=(message.content or "").strip(),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=choice.finish_reason or "",
            model=response.model,
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        await self._client.close()


def _to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue

        if message.role == "tool":
            for result in message.tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.id,
                        "content": result.content or "(no output)",
                    }
                )
            continue

        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            out.append(entry)
            continue

        if message.content:
            out.append({"role": "user", "content": message.content})
    return out
