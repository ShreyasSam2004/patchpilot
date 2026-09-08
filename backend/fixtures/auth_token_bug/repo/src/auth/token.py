"""Refresh-token handling for the session service."""

from __future__ import annotations

import time
from dataclasses import dataclass


class TokenError(Exception):
    """Base class for token problems."""

    status_code = 500


class InvalidTokenError(TokenError):
    """The token is malformed or was never issued."""

    status_code = 401


class ExpiredTokenError(TokenError):
    """The token was valid but has passed its expiry."""

    status_code = 401


@dataclass
class Token:
    value: str
    subject: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class TokenStore:
    """In-memory token store."""

    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}

    def issue(self, subject: str, value: str, ttl_s: float = 3600.0) -> Token:
        token = Token(value=value, subject=subject, expires_at=time.time() + ttl_s)
        self._tokens[value] = token
        return token

    def get(self, value: str) -> Token:
        token = self._tokens.get(value)
        if token is None:
            raise InvalidTokenError(f"unknown refresh token: {value[:8]}...")
        if token.expired:
            raise ExpiredTokenError("refresh token has expired")
        return token


def verify_refresh_token(store: TokenStore, value: str) -> Token:
    """Return the token for ``value``, raising a :class:`TokenError` subclass."""
    if not value:
        raise InvalidTokenError("refresh token is required")
    return store.get(value)
