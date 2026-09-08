"""Filesystem helpers.

``shutil.rmtree`` cannot delete a git checkout on Windows: objects under
``.git/objects`` are created read-only, and ``DeleteFile`` refuses them with
``PermissionError``. With ``ignore_errors=True`` the failure is silent and the
workspace is left on disk, so every run leaks a full checkout.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from .logging_setup import get_logger

log = get_logger(__name__)


def _make_writable(func, path, _exc_info):
    """rmtree error handler: clear the read-only bit and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def remove_tree(path: Path, *, retries: int = 3) -> bool:
    """Delete a directory tree, handling read-only files and transient locks.

    Returns True if the path is gone afterwards.
    """
    if not path.exists():
        return True

    for attempt in range(retries):
        try:
            shutil.rmtree(path, onerror=_make_writable)
        except OSError as exc:
            log.debug("fs.rmtree_retry", path=str(path), attempt=attempt, error=str(exc))
        if not path.exists():
            return True
        # A virus scanner or a just-exited test process can hold a handle open
        # briefly; a short backoff clears it in practice.
        time.sleep(0.2 * (attempt + 1))

    if path.exists():
        log.warning("fs.rmtree_failed", path=str(path))
        return False
    return True
