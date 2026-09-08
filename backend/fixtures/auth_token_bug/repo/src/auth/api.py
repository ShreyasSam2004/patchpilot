"""HTTP surface for the session service."""

from __future__ import annotations

from typing import Any

from .token import InvalidTokenError, TokenStore, verify_refresh_token


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "body": body}


def refresh_session(store: TokenStore, refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a new session.

    Returns an HTTP-shaped dict: ``{"status": int, "body": {...}}``.
    """
    try:
        token = verify_refresh_token(store, refresh_token)
    except InvalidTokenError as exc:
        return _json(401, {"error": "invalid_token", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001 - last-resort handler
        return _json(500, {"error": "internal_error", "detail": str(exc)})

    return _json(200, {"subject": token.subject, "session": f"session-for-{token.subject}"})


def logout(store: TokenStore, refresh_token: str) -> dict[str, Any]:
    try:
        verify_refresh_token(store, refresh_token)
    except InvalidTokenError as exc:
        return _json(401, {"error": "invalid_token", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001 - last-resort handler
        return _json(500, {"error": "internal_error", "detail": str(exc)})
    return _json(204, {})
