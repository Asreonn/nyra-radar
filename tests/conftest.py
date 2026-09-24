from __future__ import annotations

import pytest

from radar.config import Config, EmbeddingConfig, set_config
from radar.db import init_engine, migrate, session_factory


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
