"""Canonical identity of the repository a run targets.

Publication must go to the repository the run was actually prepared from, and
that has to be decided from the URL the user supplied at run creation -- not
re-derived later from issue text, a branch name, or anything the model touched.
Inferring a push target from model-influenced text is how an agent ends up
opening a pull request against a repository nobody asked about.

Parsing is strict on purpose: anything not recognisably a GitHub remote returns
``None`` and the run is simply not publishable, which is a much better outcome
than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: github.com remotes in the forms git actually produces.
#:   https://github.com/owner/repo(.git)
#:   http://github.com/owner/repo
#:   git@github.com:owner/repo.git
#:   ssh://git@github.com/owner/repo.git
_GITHUB = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/"
    r"|git@github\.com:"
    r"|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Where an approved patch would be published."""

    provider: str
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    def clone_url(self) -> str:
        """HTTPS clone URL, with no credentials in it.

        Credentials are supplied separately at push time and never stored on
        the run, so this value is safe to persist, log and show.
        """
        return f"https://github.com/{self.owner}/{self.repo}.git"


def parse_repository(url: str | None) -> RepositoryIdentity | None:
    """Identify a GitHub repository from a clone URL, or ``None``."""
    if not url:
        return None
    # Normalise before matching. `.git` cannot be an optional group in the
    # pattern: the repo character class allows dots, so a greedy match happily
    # consumes it and yields a repository literally named "sqlparse.git".
    cleaned = url.strip().rstrip("/")
    if cleaned.lower().endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    match = _GITHUB.match(cleaned)
    if match is None:
        return None
    return RepositoryIdentity(
        provider="github",
        owner=match.group("owner"),
        repo=match.group("repo"),
    )
