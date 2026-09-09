"""Domain logic about repairs, shared by the agent loop and the tools.

These modules answer two questions that both layers need and neither owns:
*how close is this run to a source fix*, and *is this diff actually a repair*.
They sit outside ``patchpilot.agent`` because the tools import them too, and a
tool importing the agent package pulls in the session's tool registry, which
closes an import cycle.

Nothing here performs I/O or depends on the orchestrator: both modules are pure
functions over paths, commands and diffs, which is what makes them cheap enough
to call on every tool result.
"""

from .patchquality import PatchAssessment, PatchQuality, assess_patch
from .progress import Pressure, ProgressTracker, WriteKind, classify_write, is_scratch

__all__ = [
    "PatchAssessment",
    "PatchQuality",
    "Pressure",
    "ProgressTracker",
    "WriteKind",
    "assess_patch",
    "classify_write",
    "is_scratch",
]
