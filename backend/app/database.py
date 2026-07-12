"""Database engine and session setup (SQLite by default)."""
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = os.environ.get("NEWS_DB_PATH", str(DATA_DIR / "news.db"))
# Directory the database lives in — the right home for other persistent state
# (e.g. the session-signing key), so it lands on the same mounted volume in
# production instead of an ephemeral spot inside the container image.
DB_DIR = Path(DB_PATH).resolve().parent
DB_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Tune SQLite for a web app with a background writer.

    - WAL: readers never block on the rolling poller's writes (the default
      rollback journal stalls every page load while an ingest tick commits).
    - synchronous=NORMAL: safe with WAL, much faster than FULL.
    - busy_timeout: a briefly-locked database waits instead of erroring.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
