"""Provider construction from configuration."""

from __future__ import annotations

from ..config import Settings
from ..errors import ConfigurationError
from .base import ModelProvider
from .scripted import ScriptedProvider, ScriptedTurn


def create_provider(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    script: list[ScriptedTurn] | None = None,
) -> ModelProvider:
    """Build the configured provider.

    Import of the vendor SDK is deferred to the branch that needs it, so a
    missing optional dependency only matters when that provider is selected.
    """
    kind = (provider or settings.provider).lower()
    model_name = model or settings.model

    if kind == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model_name,
            api_key=settings.api_key_for("anthropic"),
            base_url=settings.anthropic_base_url,
            effort=settings.effort,
        )

    if kind == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            model_name,
            api_key=settings.api_key_for("openai"),
            base_url=settings.openai_base_url,
        )

    if kind == "scripted":
        return ScriptedProvider(
            model_name if model else "scripted-model",
            script=script
            or [ScriptedTurn(text="scripted provider with no script", tool_calls=[])],
        )

    raise ConfigurationError(
        f"unknown provider {kind!r}; expected one of: anthropic, openai, scripted"
    )


def provider_status(settings: Settings) -> dict[str, object]:
    """Whether the configured provider can actually be used right now."""
    kind = settings.provider.lower()
    if kind == "scripted":
        return {
            "provider": kind,
            "model": settings.model,
            "ready": True,
            "is_test_double": True,
            "detail": "Scripted test double — results are not real model output.",
        }
    has_key = bool(settings.api_key_for(kind))
    return {
        "provider": kind,
        "model": settings.model,
        "ready": has_key,
        "is_test_double": False,
        "detail": (
            "API key present."
            if has_key
            else f"No API key configured. Set PATCHPILOT_{kind.upper()}_API_KEY "
            f"or {kind.upper()}_API_KEY to start real runs."
        ),
    }
