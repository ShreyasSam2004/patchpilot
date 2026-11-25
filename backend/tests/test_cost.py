"""Cost estimation.

The estimate is not decoration: it feeds the run's `max_cost_usd` budget. An
inflated price stops runs early, and an estimate that ignores cached tokens
under-reports every turn, since the system prompt is cached on all of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from patchpilot.providers.anthropic_provider import AnthropicProvider
from patchpilot.providers.base import Message, estimate_cost

M = 1_000_000


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5", 5.0 + 25.0),
        ("claude-sonnet-5", 2.0 + 10.0),
        ("claude-opus-5-5", 4.0 + 20.0),
        ("claude-sonnet-5-5", 2.0 + 10.0),
        ("claude-fable-5-2", 8.0 + 40.0),
        ("claude-haiku-4-5", 1.0 + 5.0),
        ("claude-haiku-4-5-20251001", 1.0 + 5.0),
    ],
)
def test_published_prices(model: str, expected: float) -> None:
    assert estimate_cost(model, M, M) == pytest.approx(expected)


def test_a_newer_model_is_not_priced_as_its_shorter_namesake() -> None:
    # `claude-opus-5` is a prefix of `claude-opus-5-5`; the longer entry wins.
    assert estimate_cost("claude-opus-5-5", M, 0) == pytest.approx(4.0)


def test_unknown_models_are_not_given_a_made_up_price() -> None:
    assert estimate_cost("some-future-model", M, M) is None


def test_cached_tokens_are_billed_at_their_own_rates() -> None:
    # Default: reads at 10% of input, five-minute writes at 125%.
    assert estimate_cost("claude-opus-5", 0, 0, cache_read_tokens=M) == pytest.approx(0.50)
    assert estimate_cost("claude-opus-5", 0, 0, cache_write_tokens=M) == pytest.approx(6.25)
    # Models with a listed cache-read price use it.
    assert estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=M) == pytest.approx(0.20)
    assert estimate_cost("claude-sonnet-5-5", 0, 0, cache_read_tokens=M) == pytest.approx(0.10)


def test_a_long_prompt_moves_to_the_long_context_tier() -> None:
    short = estimate_cost("claude-haiku-5-5", 50_000, 1_000)
    long = estimate_cost("claude-haiku-5-5", 50_000, 1_000, cache_read_tokens=60_000)
    assert short == pytest.approx((50_000 * 0.10 + 1_000 * 0.50) / M)
    assert long == pytest.approx((50_000 * 0.50 + 1_000 * 2.50 + 60_000 * 0.05) / M)


async def test_the_anthropic_provider_counts_cached_tokens_in_its_estimate() -> None:
    """`input_tokens` is only the uncached remainder; the rest must be billed too."""
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="test-key")

    async def create(**_: object) -> SimpleNamespace:
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(
                input_tokens=1_000,
                output_tokens=500,
                cache_read_input_tokens=7_000,
                cache_creation_input_tokens=2_000,
            ),
            stop_reason="end_turn",
            model="claude-sonnet-5",
        )

    provider._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(create=create), close=None
    )
    response = await provider.complete(system="s", messages=[Message(role="user", content="u")])

    expected = (1_000 * 2.0 + 500 * 10.0 + 7_000 * 0.2 + 2_000 * 2.5) / M
    assert response.usage.cost_usd == pytest.approx(expected)
    assert response.usage.cache_read_tokens == 7_000
    assert response.usage.cache_write_tokens == 2_000
