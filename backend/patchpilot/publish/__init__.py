"""Publishing an approved patch to a git host.

Strictly downstream of :mod:`patchpilot.review`: nothing here runs until a
human has approved a specific patch digest, and everything here is
deterministic -- the model is never consulted at publication time.

The boundary PatchPilot will not cross is expressed structurally rather than as
policy: :class:`~patchpilot.publish.ports.GitHostingPort` has no merge
operation, no branch-protection write and no review submission, so no caller
can reach one.
"""

from .branch import ProtectedBranchError, branch_name, ensure_pushable
from .identity import RepositoryIdentity, parse_repository
from .ports import CheckRun, GitHostingError, GitHostingPort, PullRequestInfo, RepositoryInfo
from .service import PublicationService, build_service

__all__ = [
    "CheckRun",
    "GitHostingError",
    "GitHostingPort",
    "ProtectedBranchError",
    "PublicationService",
    "PullRequestInfo",
    "RepositoryIdentity",
    "RepositoryInfo",
    "branch_name",
    "build_service",
    "ensure_pushable",
    "parse_repository",
]
