"""API and worker both seed default sources at boot; a concurrent insert must not crash either."""
from sqlalchemy import select

import radar.cli  # noqa: F401  (import like the real entry point; see note in commit 45b77e5)
from radar.db.models import Source
from radar.services.collect import DEFAULT_SOURCES, ensure_default_sources


def test_seeding_tolerates_a_concurrent_insert(db, monkeypatch):
    from radar.db import session_factory

    kind, name, identifier, url, config = DEFAULT_SOURCES[0]
    other = session_factory()()
    mine = session_factory()()
    # My process already checked "missing"... then the other process inserts and commits first.
    real_scalar = mine.scalar
    calls = {"n": 0}

    def scalar(stmt, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            other.add(Source(kind=kind, name=name, identifier=identifier, url=url, config=config, status="active", origin="seed"))
            other.commit()
            return None
        return real_scalar(stmt, *args, **kwargs)

    monkeypatch.setattr(mine, "scalar", scalar)
    ensure_default_sources(mine)
    mine.commit()
    rows = mine.scalars(select(Source).where(Source.kind == kind, Source.identifier == identifier)).all()
    assert len(rows) == 1
    assert mine.scalars(select(Source)).all(), "the remaining defaults were still seeded"
