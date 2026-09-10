"""Async database engine and session management.

SQLite is the local default; PostgreSQL is a URL change away. The only
dialect-aware code is here: SQLite needs WAL mode and a busy timeout so the API
and the worker can share one file without ``database is locked`` errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .models import Base

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    _, _, tail = url.partition(":///")
    if not tail or tail.startswith(":memory:"):
        return None
    return Path(tail)


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    url = settings.database_url

    path = _sqlite_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, object] = {"echo": False, "future": True}
    if url.startswith("sqlite"):
        # SQLite has no server-side pool; a single connection with WAL and a
        # busy timeout lets the API and worker interleave safely.
        kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @sa.event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine(settings)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    get_engine(settings)
    assert _sessionmaker is not None
    return _sessionmaker


def _default_literal(column: sa.Column) -> str | None:
    """A SQL literal for a column's Python-side default, if it has a scalar one.

    Only scalars: a callable default (``utcnow``, ``new_id``) cannot be rendered
    into DDL, and guessing one would write the wrong value into every existing
    row.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = getattr(default, "arg", None)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def _add_missing_columns(connection: sa.Connection) -> list[str]:
    """Add columns the models declare but an existing table lacks.

    ``create_all`` creates missing *tables* and stops there: it will not alter
    one that already exists. So adding a column to a shipped model leaves every
    existing database broken at the next query -- which is exactly what happened
    when the review columns landed on ``runs``.

    Deliberately additive only. It adds columns and backfills their defaults; it
    never drops, renames or retypes anything, so it cannot destroy data. Changes
    that need more than this need a real migration, and this function will not
    pretend otherwise -- it simply leaves the mismatch in place.
    """
    inspector = sa.inspect(connection)
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all just made it, or will
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(connection.dialect)
            literal = _default_literal(column)
            # Always nullable in DDL, even for a NOT NULL model column: existing
            # rows have no value yet, and a NOT NULL add without a default fails
            # outright. The default is backfilled immediately below, and
            # SQLAlchemy applies it to every new insert.
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
            if literal is not None:
                clause += f" DEFAULT {literal}"
            connection.exec_driver_sql(clause)
            if literal is not None:
                connection.exec_driver_sql(
                    f'UPDATE "{table.name}" SET "{column.name}" = {literal} '
                    f'WHERE "{column.name}" IS NULL'
                )
            added.append(f"{table.name}.{column.name}")
    return added


async def init_db(settings: Settings | None = None) -> None:
    """Create missing tables, and add missing columns to existing ones.

    ADR-0003: ``create_all`` is intentional for this project's scope. The models
    avoid dialect-specific types so an Alembic migration chain can be generated
    against the same metadata when moving to a managed Postgres deployment.
    Until then :func:`_add_missing_columns` covers the one schema change that
    happens in practice -- a new column on an existing table -- without which a
    model change silently breaks every database that already has rows.
    """
    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        added = await connection.run_sync(_add_missing_columns)
    if added:
        log.info("db.columns_added", columns=added)
    log.info("db.ready", url=_redact(str(engine.url)))


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """A transactional session: commit on success, roll back on failure."""
    maker = get_sessionmaker(settings)
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}"
