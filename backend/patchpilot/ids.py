"""Identifier helpers.

Runs get a UUID primary key plus a short, human-quotable display code
(``A82F``) that appears in the UI header and in logs.
"""

from __future__ import annotations

import secrets
import uuid

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"  # no I/O to avoid transcription errors


def new_id() -> str:
    return uuid.uuid4().hex


def short_code(length: int = 4) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
