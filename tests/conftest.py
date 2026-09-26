from __future__ import annotations

import pytest

from radar.config import Config, EmbeddingConfig, set_config
from radar.db import init_engine, migrate, session_factory


@pytest.fixture(autouse=True)
def private_runtime(tmp_path, monkeypatch):
    """Cooldown stamps and caches of one test never leak into another (or into a real install)."""
    monkeypatch.setenv("RADAR_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("RADAR_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture
def db(tmp_path):
    init_engine(tmp_path / "test.db")
    migrate()
    set_config(Config(embeddings=EmbeddingConfig(provider="hash")))
    session = session_factory()()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
