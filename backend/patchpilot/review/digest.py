"""Binding an approval to one exact patch.

A reviewer approves *a specific diff*, not "whatever the run produces". If the
patch can change between approval and publication -- another revision lands, a
retry regenerates it -- then the thing published is not the thing approved, and
the human sign-off is worthless. The digest is what makes that detectable.

Canonicalisation is deliberately minimal: line endings and trailing whitespace
at the very end of the patch are normalised, because those change when a diff
round-trips through a database column or an editor without changing what the
patch *does*. Nothing else is touched. In particular hunk content, context
lines and file modes are hashed verbatim, so any real change to the patch --
including one that only adds a blank line inside a hunk -- produces a different
digest and invalidates the approval.
"""

from __future__ import annotations

import hashlib


def canonical_patch(diff: str) -> str:
    """Normalise only what can change without changing the patch's meaning."""
    # \r\n -> \n (database and editor round-trips), and drop a trailing blank
    # line, which `git diff` may or may not emit depending on how it was read.
    text = diff.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n" if text.strip() else ""


def patch_digest(diff: str) -> str:
    """SHA-256 of the canonical patch, as lowercase hex.

    An empty or whitespace-only patch digests to the empty string rather than to
    the hash of "", so an absent patch can never accidentally match an approved
    one.
    """
    canonical = canonical_patch(diff)
    if not canonical:
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digests_match(approved: str, candidate: str) -> bool:
    """True when a candidate patch is byte-identical to what was approved.

    An empty approved digest never matches, so "nothing was approved" cannot be
    mistaken for "this matches".
    """
    if not approved or not candidate:
        return False
    return approved == candidate
