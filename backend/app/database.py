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
    # Before journal_mode, and that ordering is the whole trick: setting the
    # journal mode writes the database header, and auto_vacuum can only be
    # chosen while the file has no header yet. Set it second and it silently
    # stays 0 on a brand-new database — verified by watching exactly that.
    cur.execute("PRAGMA auto_vacuum=INCREMENTAL")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    # Deleting rows never shrinks a SQLite file on its own; freed pages are
    # reused inside it and the file keeps its high-water mark, which is how a
    # volume fills while retention appears to be working. Incremental
    # auto-vacuum lets those pages be handed back (see storage.reclaim).
    #
    # This only takes effect on a database with no tables yet, so a new
    # deployment gets it for free. Converting an existing one requires a full
    # VACUUM, which rewrites the file while blocking every reader — minutes of
    # downtime on a large database — so that is an operator action, not
    # something to do behind their back on the next restart.
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
