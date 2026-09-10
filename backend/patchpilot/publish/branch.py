"""Branch naming, and the branches PatchPilot must never touch.

The refusal list here is enforced in the backend rather than trusted to the
caller or to GitHub's branch protection. Protection may be absent, misconfigured
or scoped to a pattern that misses the default branch of a fork -- and "the
remote would have stopped us" is not a safety argument when the operation is a
force-push to main.
"""

from __future__ import annotations

import re

#: Branch names PatchPilot refuses to write to under any circumstances, on top
#: of whatever the repository reports as its default branch.
NEVER_PUSH_TO: frozenset[str] = frozenset(
    {
        "main",
        "master",
        "trunk",
        "develop",
        "development",
        "release",
        "production",
        "prod",
        "stable",
        "gh-pages",
    }
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_PREFIX = "patchpilot"


class ProtectedBranchError(Exception):
    """Raised when something tries to write to a branch we will not touch."""


def slugify(text: str, *, limit: int = 40) -> str:
    """A short, git-safe fragment of a title. May be empty."""
    slug = _SLUG_STRIP.sub("-", (text or "").lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rstrip("-")
    return slug


def branch_name(
    *,
    run_id: str,
    issue_number: str | None = None,
    title: str | None = None,
) -> str:
    """Deterministic branch name for a run.

    Deterministic matters for idempotency: a retried publication must compute
    the same name, so it can recognise its own earlier branch instead of
    creating a second one.
    """
    parts = [_PREFIX]
    slug = slugify(title or "")
    if issue_number:
        parts.append(f"issue-{slugify(str(issue_number), limit=20) or issue_number}")
        if slug:
            parts.append(slug)
    elif slug:
        parts.append(slug)
        parts.append(f"run-{run_id[:8]}")
    else:
        parts.append(f"run-{run_id[:8]}")
    name = "/".join(parts[:2]) + ("-" + "-".join(parts[2:]) if len(parts) > 2 else "")
    return name.strip("-/")


def collision_name(base: str, attempt: int) -> str:
    """Deterministic name for the nth collision of ``base``.

    Suffixes rather than force-pushes. An existing branch of the same name may
    be someone else's work, or an earlier publication whose PR is still open;
    overwriting either would destroy work that is not ours to destroy.
    """
    return base if attempt <= 0 else f"{base}-{attempt + 1}"


def ensure_pushable(branch: str, *, default_branch: str | None = None) -> str:
    """Return ``branch`` if PatchPilot may write to it, else raise.

    Checked immediately before every push, not only at name generation, so a
    branch that arrives from persisted state or a retry is validated too.
    """
    candidate = (branch or "").strip().removeprefix("refs/heads/")
    if not candidate:
        raise ProtectedBranchError("refusing to push to an empty branch name")
    lowered = candidate.lower()
    if lowered in NEVER_PUSH_TO:
        raise ProtectedBranchError(f"refusing to push to protected branch {candidate!r}")
    if default_branch and lowered == default_branch.strip().lower():
        raise ProtectedBranchError(
            f"refusing to push to the repository's default branch {candidate!r}"
        )
    if not candidate.startswith(f"{_PREFIX}/"):
        raise ProtectedBranchError(
            f"refusing to push to {candidate!r}: PatchPilot only writes branches "
            f"under {_PREFIX}/"
        )
    return candidate
