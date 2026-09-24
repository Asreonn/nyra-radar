"""Alembic environment. Uses the Radar DB path unless a connection is supplied."""
from __future__ import annotations

from alembic import context

from radar.db.engine import make_engine
from radar.db.models import Base
from radar import paths

config = context.config
target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    # FTS5 shadow tables are managed by raw DDL in the migrations.
    if type_ == "table" and name and name.startswith("item_fts"):
        return False
    return True


def run() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                          include_object=_include_object)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = config.get_main_option("sqlalchemy.url")
    engine = make_engine(url.removeprefix("sqlite:///") if url else paths.db_path())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True,
                          include_object=_include_object)
        with context.begin_transaction():
            context.run_migrations()


run()
