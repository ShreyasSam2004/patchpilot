"""Run budgets.

Every autonomous loop needs a reason to stop that does not depend on the model
choosing to. :class:`BudgetTracker` owns all of them: iterations, tool calls,
wall-clock time, and estimated spend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Settings


@dataclass(slots=True)
class Budget:
    max_iterations: int = 30
    max_tool_calls: int = 160
    max_runtime_s: int = 1800
    max_cost_usd: float = 5.0

    @classmethod
    def from_settings(cls, settings: Settings) -> Budget:
        return cls(
            max_iterations=settings.max_iterations,
            max_tool_calls=settings.max_tool_calls,
            max_runtime_s=settings.max_runtime_s,
            max_cost_usd=settings.max_cost_usd,
        )

    def merged(self, overrides: dict[str, object] | None) -> Budget:
        """Apply per-run overrides, ignoring unknown or non-positive values."""
        if not overrides:
            return self
        merged = Budget(**{f: getattr(self, f) for f in self.__slots__})  # type: ignore[attr-defined]
        for key, value in overrides.items():
            if key in self.__slots__ and isinstance(value, (int, float)) and value > 0:  # type: ignore[attr-defined]
                setattr(merged, key, value)
        return merged


@dataclass
class BudgetTracker:
    """Live budget accounting for a single run."""

    budget: Budget
    started_at: float = field(default_factory=time.monotonic)
    iterations: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.budget.max_runtime_s - self.elapsed_s)

    def start_iteration(self) -> None:
        self.iterations += 1

    def record_tool_call(self, count: int = 1) -> None:
        self.tool_calls += count

    def record_cost(self, cost_usd: float | None) -> None:
        if cost_usd:
            self.cost_usd += cost_usd

    def exceeded(self) -> str | None:
        """Name of the first exhausted limit, or ``None`` while budget remains."""
        if self.iterations >= self.budget.max_iterations:
            return "max_iterations"
        if self.tool_calls >= self.budget.max_tool_calls:
            return "max_tool_calls"
        if self.elapsed_s >= self.budget.max_runtime_s:
            return "max_runtime_s"
        if self.budget.max_cost_usd > 0 and self.cost_usd >= self.budget.max_cost_usd:
            return "max_cost_usd"
        return None

    def snapshot(self) -> dict[str, object]:
        return {
            "iterations": self.iterations,
            "max_iterations": self.budget.max_iterations,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.budget.max_tool_calls,
            "elapsed_s": round(self.elapsed_s, 1),
            "max_runtime_s": self.budget.max_runtime_s,
            "cost_usd": round(self.cost_usd, 4),
            "max_cost_usd": self.budget.max_cost_usd,
        }

    def describe_remaining(self) -> str:
        """Text the agent sees, so it can pace itself rather than being cut off."""
        return (
            f"iteration {self.iterations}/{self.budget.max_iterations}, "
            f"{self.budget.max_tool_calls - self.tool_calls} tool calls left, "
            f"{int(self.remaining_s)}s of wall-clock budget left"
        )
