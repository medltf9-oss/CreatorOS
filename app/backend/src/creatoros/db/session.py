"""Engine and session management.

SQLite is configured for a single-writer, multi-reader desktop workload:
WAL journal, foreign keys on, a 5 s busy timeout, and a short-lived session per
unit of work. The database file lives under the configured D: root only.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..core.paths import ensure_dir
from .base import Base

_engine: sa.Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_path() -> object:
    return ensure_dir("database") / "creatoros.db"


def _configure(engine: sa.Engine) -> None:
    @sa.event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()


def get_engine() -> sa.Engine:
    global _engine
    if _engine is None:
        path = database_path()
        _engine = sa.create_engine(
            f"sqlite+pysqlite:///{path}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_pre_ping=True,
        )
        _configure(_engine)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """Create tables directly (tests only - production uses Alembic)."""
    Base.metadata.create_all(get_engine())


def drop_all() -> None:
    Base.metadata.drop_all(get_engine())


def reset_engine() -> None:
    """Dispose and forget the engine (used by tests that switch data roots)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
