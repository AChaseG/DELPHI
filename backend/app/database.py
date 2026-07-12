"""Database engine and session setup (SQLite by default)."""
import os
from pathlib import Path

from sqlalchemy import create_engine
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

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
