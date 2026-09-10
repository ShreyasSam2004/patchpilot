"""Review state machine.

The transition table lives in :mod:`patchpilot.domain.enums` next to the states
themselves; this module is the guard that enforces it. Every state change goes
through :func:`ensure_transition`, so an invalid move is an explicit error at
the point it is attempted rather than a workflow that quietly ends up somewhere
impossible -- "published without approval" being the one that matters.
"""

from __future__ import annotations

from ..domain.enums import REVIEW_TRANSITIONS, ReviewState
from ..errors import PatchPilotError


class InvalidReviewTransition(PatchPilotError):
    """Raised when a review state change is not allowed."""

    def __init__(self, current: ReviewState, target: ReviewState) -> None:
        allowed = sorted(str(s) for s in REVIEW_TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"cannot move review from {current} to {target}; "
            f"allowed from {current}: {', '.join(allowed) or 'nothing (terminal)'}"
        )
        self.current = current
        self.target = target


def can_transition(current: ReviewState, target: ReviewState) -> bool:
    return target in REVIEW_TRANSITIONS.get(current, frozenset())


def ensure_transition(current: ReviewState, target: ReviewState) -> ReviewState:
    """Return ``target`` if the move is legal, else raise.

    Self-transitions are allowed and are a no-op: they are what a double-clicked
    button looks like, and rejecting them would turn an idempotent action into
    an error the user has to interpret.
    """
    if current is target:
        return target
    if not can_transition(current, target):
        raise InvalidReviewTransition(current, target)
    return target
