"""Which of a patch's paths are GitHub Actions workflows.

The reading of the patch itself lives in :mod:`patchpilot.diffutil`, because the
workspace needs the same answer when it refuses a write. What belongs here is
the publication-specific question on top of it: a commit that touches a workflow
the approved patch does not is refused before anything is pushed, and a push
that GitHub rejects for workflow reasons is explained in those terms.

``changed_paths`` is re-exported so publication code has a single import.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..diffutil import changed_paths

#: Where GitHub keeps Actions definitions. Pushing a ref whose tree differs from
#: the destination's default branch *here* is what triggers the server-side
#: workflow permission check, so this prefix gets its own error path.
WORKFLOW_DIR = ".github/workflows/"


def workflow_paths(paths: Iterable[str]) -> frozenset[str]:
    """The subset of ``paths`` that are GitHub Actions workflow definitions."""
    return frozenset(p for p in paths if p.startswith(WORKFLOW_DIR))


__all__ = ["WORKFLOW_DIR", "changed_paths", "workflow_paths"]
