"""SQLite engine (WAL), session factory, FTS5 DDL and schema bootstrap."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from radar import paths

log = logging.getLogger(__name__)

FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5("
    "title, body, summary, topics, source, extra, "
    "tokenize = 'porter unicode61 remove_diacritics 2')"
)

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def _set_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    for pragma in (
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=30000",
        "PRAGMA temp_store=MEMORY",
        "PRAGMA cache_size=-16000",
        "PRAGMA mmap_size=134217728",
    ):
        cur.execute(pragma)
    cur.close()


def make_engine(db_file: Path | str) -> Engine:
    url = "sqlite://" if str(db_file) == ":memory:" else f"sqlite:///{db_file}"
    engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})
    event.listen(engine, "connect", _set_pragmas)
    return engine


def init_engine(db_file: Path | str | None = None) -> Engine:
    """Create (or replace) the process-wide engine."""
    global _engine, _factory
    if db_file is None:
        paths.ensure_dirs()
        db_file = paths.db_path()
    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(db_file)
    _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _factory is None:
        init_engine()
    assert _factory is not None
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema(engine: Engine | None = None) -> None:
    """Create all tables directly (tests / dev). Production uses Alembic migrations."""
    from radar.db.models import Base

    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))


def migrate(engine: Engine | None = None) -> None:
    """Run Alembic migrations to head (idempotent)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    engine = engine or get_engine()
    root = Path(__file__).resolve().parents[2]
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
