"""Human review of generated patches.

The workflow between "the agent produced a patch" and "a branch exists on
GitHub". Nothing here talks to GitHub; publication is
:mod:`patchpilot.publish`, which refuses to act without a decision recorded
here.
"""

from .digest import canonical_patch, digests_match, patch_digest
from .states import InvalidReviewTransition, can_transition, ensure_transition

__all__ = [
    "InvalidReviewTransition",
    "can_transition",
    "canonical_patch",
    "digests_match",
    "ensure_transition",
    "patch_digest",
]
