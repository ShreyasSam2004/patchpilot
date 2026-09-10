"""Adding a column to a shipped model must not break existing databases.

`create_all` creates missing *tables* and stops: it will not alter one that
already exists. So when the review columns landed on `runs`, every database with
rows in it started failing on the next query with

    sqlalchemy.exc.OperationalError: no such column: runs.review_state

These tests pin the additive upgrade that fixes it, and -- just as importantly
-- pin that it is only ever additive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlalchemy as sa

from patchpilot.config import Settings
from patchpilot.db.models import Base
from patchpilot.db.session import _add_missing_columns, _default_literal, dispose_db, init_db


def _url(path: Path) -> str:
    return "sqlite+aiosqlite:///" + path.resolve().as_posix()


def _legacy_database(path: Path) -> None:
    """A `runs` table as it looked before the review columns existed."""
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE runs (
            id VARCHAR(32) NOT NULL PRIMARY KEY,
            code VARCHAR(8),
            repository VARCHAR(500),
            issue TEXT,
            state VARCHAR(20),
            stop_reason TEXT,
            summary TEXT,
            final_patch TEXT,
            last_seq INTEGER,
            queued_at DATETIME
        )
        """
    )
    connection.execute(
        "INSERT INTO runs (id, code, repository, issue, state, last_seq) "
        "VALUES ('old-run-1', 'OLD1', 'owner/repo', 'a bug', 'RESOLVED', 12)"
    )
    connection.commit()
    connection.close()


async def test_an_existing_database_gains_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path)

    await init_db(Settings(database_url=_url(path)))
    await dispose_db()

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    for added in (
        "review_state",
        "approved_patch_sha256",
        "approved_at",
        "approved_by",
        "repo_provider",
        "repo_owner",
        "repo_name",
        "base_branch",
    ):
        assert added in columns, f"{added} was not added"
    connection.close()


async def test_existing_rows_survive_and_are_backfilled(tmp_path: Path) -> None:
    """The upgrade must never cost data."""
    path = tmp_path / "legacy.db"
    _legacy_database(path)

    await init_db(Settings(database_url=_url(path)))
    await dispose_db()

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT id, code, state, last_seq, review_state FROM runs"
    ).fetchone()
    assert row[:4] == ("old-run-1", "OLD1", "RESOLVED", 12), "existing values unchanged"
    assert row[4] == "RUNNING", "the model's default is backfilled, not left NULL"
    assert connection.execute(
        "SELECT COUNT(*) FROM runs WHERE review_state IS NULL"
    ).fetchone()[0] == 0
    connection.close()


async def test_the_new_tables_are_created_too(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path)

    await init_db(Settings(database_url=_url(path)))
    await dispose_db()

    connection = sqlite3.connect(path)
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"patch_versions", "publications", "review_audit"} <= tables
    connection.close()


async def test_running_it_twice_changes_nothing(tmp_path: Path) -> None:
    """init_db runs on every process start, so it must be idempotent."""
    path = tmp_path / "legacy.db"
    _legacy_database(path)

    await init_db(Settings(database_url=_url(path)))
    await dispose_db()
    connection = sqlite3.connect(path)
    first = [row[1] for row in connection.execute("PRAGMA table_info(runs)")]
    connection.close()

    await init_db(Settings(database_url=_url(path)))
    await dispose_db()
    connection = sqlite3.connect(path)
    second = [row[1] for row in connection.execute("PRAGMA table_info(runs)")]
    assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    connection.close()

    assert first == second, "a second run must add nothing"


def test_the_upgrade_never_drops_or_retypes_anything(tmp_path: Path) -> None:
    """A column the models no longer declare is left alone, not dropped.

    Additive-only is the safety property: an upgrade that could remove a column
    could destroy data, and this one structurally cannot.
    """
    path = tmp_path / "extra.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runs (id VARCHAR(32) PRIMARY KEY, "
        "a_column_the_models_forgot TEXT, state VARCHAR(20))"
    )
    connection.execute(
        "INSERT INTO runs (id, a_column_the_models_forgot, state) "
        "VALUES ('r1', 'precious', 'RESOLVED')"
    )
    connection.commit()
    connection.close()

    engine = sa.create_engine("sqlite:///" + path.resolve().as_posix())
    with engine.begin() as sync:
        Base.metadata.create_all(sync)
        _add_missing_columns(sync)
    engine.dispose()

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    assert "a_column_the_models_forgot" in columns, "an unknown column must survive"
    assert connection.execute(
        "SELECT a_column_the_models_forgot FROM runs"
    ).fetchone()[0] == "precious"
    connection.close()


def test_only_scalar_defaults_are_rendered_into_ddl() -> None:
    """A callable default cannot be rendered, and guessing would be worse.

    `utcnow` and `new_id` would each write one identical wrong value into every
    existing row, so those columns are added nullable and left alone instead.
    """
    runs = Base.metadata.tables["runs"]
    assert _default_literal(runs.c.review_state) == "'RUNNING'"
    assert _default_literal(runs.c.queued_at) is None, "callable default"
    assert _default_literal(runs.c.cancel_requested) == "0", "booleans render as 0/1"
    assert _default_literal(runs.c.last_seq) == "0"
    assert _default_literal(runs.c.id) is None, "new_id is callable"


def test_a_string_default_containing_a_quote_is_escaped() -> None:
    column = sa.Column("x", sa.String(10), default="it's")
    assert _default_literal(column) == "'it''s'"
