"""Shared pytest fixtures.

Every test runs against a throwaway SQLite database and with ingestion,
rate limiting, translation, and city seeding switched off — the env is set
*before* the app modules import so the engine binds to the temp DB.
"""
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="delphi-tests-"), "test.db")
os.environ.update(
    NEWS_DB_PATH=_TMP_DB,
    NEWS_DISABLE_INGEST="1",
    NEWS_SEED_CITIES="0",
    NEWS_RATE_LIMIT="0",
    NEWS_CONTENT_FETCH="0",
    NEWS_TRANSLATE_PROVIDER="off",
    NEWS_SECRET="test-secret-key",
)

from backend.app import home  # noqa: E402
from backend.app.database import Base, engine, SessionLocal  # noqa: E402
from backend.app.main import _ensure_schema  # noqa: E402

Base.metadata.create_all(engine)
_ensure_schema()


@pytest.fixture
def db():
    """A session against a schema reset to empty for the test."""
    for table in reversed(Base.metadata.sorted_tables):
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM {table.name}")
    home.clear()   # a warm Home board must not outlive the rows it was built from
    from backend.app import main as _main
    _main._invalidate_sources_cache()
    _main._invalidate_stats_cache()
    _main._DB_STATE = (0.0, True, "")
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    """FastAPI TestClient with the DB reset (no lifespan seeding needed)."""
    for table in reversed(Base.metadata.sorted_tables):
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM {table.name}")
    home.clear()
    from backend.app import main as _main
    _main._invalidate_sources_cache()   # rendered per process; a fresh table needs a fresh one
    # Same for the dashboard's headline numbers: they are counted once a minute
    # and shared, so an emptied table must not be described by the last test's
    # totals.
    _main._invalidate_stats_cache()
    # /healthz remembers its last answer between checks; a test that breaks the
    # database must not be handed a previous test's "ok".
    _main._DB_STATE = (0.0, True, "")
    from fastapi.testclient import TestClient
    from backend.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def register(client):
    """Returns register(username) -> Authorization header for a new account."""
    def _register(username="alice", email=None, password="correct-horse-staple"):
        r = client.post("/api/auth/register", json={
            "username": username, "email": email or f"{username}@example.com",
            "password": password})
        assert r.status_code == 201, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}
    return _register
