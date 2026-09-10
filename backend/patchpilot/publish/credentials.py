"""GitHub credentials, kept entirely inside the deterministic backend.

Nothing here is ever placed in a prompt, a tool result, an event payload, an
API response or a log line. The agent's sandbox has no network and its command
guard blocks `git push`, `git remote` and `git credential`, so there is no path
by which the model could reach a token even if one leaked into the workspace --
but the simplest guarantee is that it never leaves this module except as an
``Authorization`` header on an outbound request.

Two modes, in order of preference:

**GitHub App** (production). A short-lived installation token is minted from
the App's private key and cached until shortly before it expires. Tokens are
scoped to the installation and expire in an hour, so a leaked one has a small
blast radius and cannot be used against other repositories.

**Personal access token** (development). Simpler, long-lived, broader scope.
Accepted because requiring an App to try the feature locally is a poor trade,
but the App path is the one designed for real use.

Minimum App permissions -- request no more:

    Repository -> Contents        Read & write   (branches, commits, push)
    Repository -> Pull requests   Read & write   (open the PR)
    Repository -> Metadata        Read-only      (mandatory)
    Repository -> Checks          Read-only      (CI status display only)

Explicitly *not* requested: Administration, Workflows, Members, or anything
granting merge or branch-protection bypass. PatchPilot opens pull requests; the
engineering team's own process merges them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import Settings
from ..domain.enums import PublishErrorCode
from ..logging_setup import get_logger
from .ports import GitHostingError

log = get_logger(__name__)

GITHUB_API = "https://api.github.com"

#: Refresh an installation token this long before it actually expires, so a
#: request never starts with a token that dies mid-flight.
_EXPIRY_MARGIN_S = 300


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at - _EXPIRY_MARGIN_S


class GitHubCredentials:
    """Supplies a bearer token, from a GitHub App or a PAT."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cached: _CachedToken | None = None

    # -- configuration ------------------------------------------------------
    @property
    def app_configured(self) -> bool:
        s = self._settings
        return bool(s.github_app_id and s.github_installation_id and self._private_key_text())

    @property
    def token_configured(self) -> bool:
        return bool(self._settings.github_token)

    @property
    def configured(self) -> bool:
        return self.app_configured or self.token_configured

    @property
    def mode(self) -> str:
        if self.app_configured:
            return "github_app"
        if self.token_configured:
            return "personal_access_token"
        return "unconfigured"

    def describe(self) -> dict[str, object]:
        """Safe to return from the API: says how auth is set up, never what it is."""
        return {
            "configured": self.configured,
            "mode": self.mode,
            "app_id_present": bool(self._settings.github_app_id),
            "installation_id_present": bool(self._settings.github_installation_id),
            "private_key_present": bool(self._private_key_text()),
            "token_present": bool(self._settings.github_token),
        }

    # -- token minting ------------------------------------------------------
    async def bearer_token(self) -> str:
        if self.app_configured:
            return await self._installation_token()
        if self.token_configured:
            return self._settings.github_token or ""
        raise GitHostingError(
            PublishErrorCode.NOT_CONFIGURED,
            "GitHub integration is not configured. Set a GitHub App "
            "(app id, installation id, private key) or a personal access token.",
        )

    def _private_key_text(self) -> str | None:
        s = self._settings
        if s.github_app_private_key:
            return s.github_app_private_key
        if s.github_app_private_key_path:
            path = Path(s.github_app_private_key_path)
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("github.private_key_unreadable", error=str(exc))
                return None
        return None

    def _app_jwt(self) -> str:
        """Short-lived JWT proving we are the App. Never leaves this process."""
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise GitHostingError(
                PublishErrorCode.NOT_CONFIGURED,
                "GitHub App authentication needs PyJWT with cryptography installed.",
            ) from exc

        key = self._private_key_text()
        if not key:
            raise GitHostingError(
                PublishErrorCode.NOT_CONFIGURED, "GitHub App private key is not readable."
            )
        now = int(time.time())
        payload = {
            # Backdated by a minute to tolerate clock skew, which GitHub rejects.
            "iat": now - 60,
            "exp": now + 480,
            "iss": str(self._settings.github_app_id),
        }
        try:
            return jwt.encode(payload, key, algorithm="RS256")
        except Exception as exc:
            raise GitHostingError(
                PublishErrorCode.AUTH_FAILED,
                "Could not sign the GitHub App request; check the private key format.",
            ) from exc

    async def _installation_token(self) -> str:
        if self._cached is not None and self._cached.usable:
            return self._cached.value

        installation = self._settings.github_installation_id
        url = f"{GITHUB_API}/app/installations/{installation}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHostingError(
                PublishErrorCode.AUTH_FAILED, f"Could not reach GitHub to mint a token: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise GitHostingError(
                PublishErrorCode.AUTH_FAILED,
                "GitHub rejected the App credentials. Check the app id, installation "
                "id and private key.",
            )
        if response.status_code >= 400:
            raise GitHostingError(
                PublishErrorCode.AUTH_FAILED,
                f"GitHub returned {response.status_code} when minting an installation token.",
            )

        body = response.json()
        token = body.get("token", "")
        expires = body.get("expires_at")
        expires_at = time.time() + 3600
        if isinstance(expires, str):
            from datetime import datetime

            try:
                parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                expires_at = parsed.timestamp()
            except ValueError:
                pass
        self._cached = _CachedToken(value=token, expires_at=expires_at)
        log.info("github.installation_token_minted", mode="github_app")
        return token
