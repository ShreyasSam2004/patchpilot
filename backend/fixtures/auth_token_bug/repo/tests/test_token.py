import time

import pytest

from src.auth.api import logout, refresh_session
from src.auth.token import (
    ExpiredTokenError,
    InvalidTokenError,
    TokenStore,
    verify_refresh_token,
)


@pytest.fixture
def store():
    return TokenStore()


def test_valid_token_refreshes(store):
    store.issue("alice", "tok-valid", ttl_s=60)
    response = refresh_session(store, "tok-valid")
    assert response["status"] == 200
    assert response["body"]["subject"] == "alice"


def test_unknown_token_is_401(store):
    response = refresh_session(store, "tok-unknown")
    assert response["status"] == 401
    assert response["body"]["error"] == "invalid_token"


def test_missing_token_is_401(store):
    response = refresh_session(store, "")
    assert response["status"] == 401


def test_expired_token_is_401_not_500(store):
    store.issue("bob", "tok-expired", ttl_s=-1)
    response = refresh_session(store, "tok-expired")
    assert response["status"] == 401, "an expired refresh token must not be a server error"
    assert response["body"]["error"] in {"invalid_token", "expired_token"}


def test_expired_token_on_logout_is_401_not_500(store):
    store.issue("bob", "tok-expired-2", ttl_s=-1)
    response = logout(store, "tok-expired-2")
    assert response["status"] == 401


def test_logout_with_valid_token(store):
    store.issue("carol", "tok-logout", ttl_s=60)
    assert logout(store, "tok-logout")["status"] == 204


def test_store_raises_expired(store):
    store.issue("dave", "tok-x", ttl_s=-1)
    with pytest.raises(ExpiredTokenError):
        verify_refresh_token(store, "tok-x")


def test_store_raises_invalid(store):
    with pytest.raises(InvalidTokenError):
        verify_refresh_token(store, "nope")


def test_token_expiry_flag(store):
    token = store.issue("erin", "tok-y", ttl_s=60)
    assert not token.expired
    token.expires_at = time.time() - 1
    assert token.expired
