"""A small arithmetic helper used by the reporting pipeline."""

from __future__ import annotations


def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


def subtract(a: float, b: float) -> float:
    """Return the difference of two numbers."""
    return a - b


def multiply(a: float, b: float) -> float:
    """Return the product of two numbers."""
    return a * b


def divide(a: float, b: float) -> float | None:
    """Return ``a / b``.

    Division by zero is not an error for callers of this module: the reporting
    pipeline treats an undefined ratio as a missing value.
    """
    return a / b


def average(values: list[float]) -> float | None:
    """Return the mean of ``values``, or ``None`` for an empty list."""
    if not values:
        return None
    return sum(values) / len(values)


def percentage(part: float, whole: float) -> float | None:
    """Return ``part`` as a percentage of ``whole``."""
    ratio = divide(part, whole)
    if ratio is None:
        return None
    return ratio * 100
