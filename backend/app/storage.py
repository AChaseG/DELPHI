"""Knowing how much room is left, and giving it back.

Delphi filled its 1 GB volume and the app died on the next restart — SQLite
could not even enable WAL, so it never opened a port and Fly stopped routing to
it. Retention had been deleting old articles the whole time and it made no
difference, for a reason that is easy to miss:

**Deleting rows does not shrink a SQLite database.** Freed pages are marked
reusable inside the file; the file keeps its high-water mark forever. So the
archive grew to fill the disk during the busiest stretch and then stayed there,
however much was pruned afterwards.

The usual answer, VACUUM, cannot rescue that situation: it writes a complete
second copy, so it needs about as much free space as the database is big, which
is exactly what a full disk does not have. Same for switching auto_vacuum on,
which needs one VACUUM to take effect.

So this module does two separate jobs:

*Convert, once, while there is room.* Set auto_vacuum=INCREMENTAL and VACUUM —
guarded on there being enough free space to survive it, and skipped otherwise
with a message rather than a crash.

*Then hand pages back continuously.* With INCREMENTAL in force,
`PRAGMA incremental_vacuum` returns freed pages to the filesystem without
rewriting anything, so the file tracks the data rather than the peak.

And it measures, because the fault before this was not that the disk filled —
it was that nothing said so until the process would not start.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .database import DB_PATH, engine

log = logging.getLogger("storage")

# Below this much free space, ingestion stops adding to the problem. Sized to
# leave room for the WAL to check-point and for the database to be opened at
# all on the next restart — the thing that actually failed.
LOW_SPACE_MB = float(os.environ.get("NEWS_LOW_SPACE_MB", "128"))

# How much of the volume the archive may occupy. The default is a fraction
# rather than a number of megabytes so it adapts to whatever volume the
# operator provisions — 1 GB or 20 — instead of needing to be reset each time.
DB_MAX_FRACTION = float(os.environ.get("NEWS_DB_MAX_FRACTION", "0.7"))
# An explicit ceiling in MB wins over the fraction when set.
DB_MAX_MB = float(os.environ.get("NEWS_DB_MAX_MB", "0"))

# Pages handed back per incremental pass. 2,000 pages is ~8 MB at the default
# page size: enough to keep up with pruning, small enough not to stall a tick.
VACUUM_PAGES = int(os.environ.get("NEWS_VACUUM_PAGES", "2000"))


def _paths() -> list[Path]:
    """The database and the files SQLite keeps beside it."""
    base = Path(DB_PATH)
    return [base, Path(f"{base}-wal"), Path(f"{base}-shm")]


def db_bytes() -> int:
    """Everything the database occupies, including a WAL that can be large."""
    total = 0
    for path in _paths():
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def disk() -> dict:
    """Room on the volume the database lives on, and how much of it is ours."""
    try:
        usage = shutil.disk_usage(Path(DB_PATH).resolve().parent)
    except OSError as exc:
        return {"ok": False, "detail": f"could not read disk usage: {exc}"}
    database = db_bytes()
    return {
        "ok": True,
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "db_bytes": database,
        "free_pct": round(usage.free / usage.total * 100, 1) if usage.total else 0.0,
        "db_pct_of_volume": round(database / usage.total * 100, 1) if usage.total else 0.0,
        "low": usage.free < LOW_SPACE_MB * 1024 * 1024,
    }


def db_ceiling() -> int:
    """How large the archive is allowed to get, in bytes.

    From the volume rather than a fixed number, so provisioning a bigger disk
    raises the ceiling by itself. Anything else on the volume is already
    excluded, because this is measured against the total and the archive is
    almost all of what is there.
    """
    if DB_MAX_MB > 0:
        return int(DB_MAX_MB * 1024 * 1024)
    info = disk()
    if not info["ok"]:
        return 0        # unknown: no ceiling rather than a wrong one
    return int(info["total_bytes"] * DB_MAX_FRACTION)


def over_ceiling() -> int:
    """Bytes above the ceiling, or 0 when within it."""
    ceiling = db_ceiling()
    if ceiling <= 0:
        return 0
    return max(0, db_bytes() - ceiling)


def auto_vacuum_mode() -> int:
    """0 NONE, 1 FULL, 2 INCREMENTAL."""
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("PRAGMA auto_vacuum").scalar() or 0)


def ensure_incremental_vacuum() -> dict:
    """Convert the database to incremental auto-vacuum, once, if it is safe.

    Only worth doing while there is room: the conversion VACUUM writes a whole
    second copy. Refusing loudly beats filling the disk trying to fix the disk.
    """
    try:
        mode = auto_vacuum_mode()
    except Exception as exc:                       # a database we cannot read
        return {"converted": False, "reason": f"could not read auto_vacuum: {exc}"}
    if mode == 2:
        return {"converted": False, "reason": "already incremental"}

    info = disk()
    needed = db_bytes()
    if info["ok"] and info["free_bytes"] < needed * 1.1:
        # The situation this whole module exists because of. Say so plainly:
        # the operator has to add space before anything here can help.
        return {"converted": False,
                "reason": f"not enough free space to convert safely — VACUUM needs "
                          f"~{needed / 1e6:.0f} MB free and there is "
                          f"{info['free_bytes'] / 1e6:.0f} MB"}

    log.info("converting database to incremental auto-vacuum (%.0f MB) — "
             "this holds a write lock while it runs", needed / 1e6)
    with _autocommit() as conn:
        conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
        conn.exec_driver_sql("VACUUM")
    log.info("conversion done; freed pages now return to the filesystem "
             "(%.0f MB on disk)", db_bytes() / 1e6)
    return {"converted": True, "reason": "converted to incremental"}


def _autocommit():
    """A connection with no transaction wrapped around it.

    VACUUM and incremental_vacuum cannot do their work inside a transaction,
    and SQLAlchemy opens one on the first statement of a normal connection.
    Measured, rather than assumed: run this way, `PRAGMA incremental_vacuum`
    returned exactly one page out of four thousand waiting — succeeding,
    reporting nothing, and freeing nothing.
    """
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def reclaim(pages: int = 0) -> int:
    """Hand freed pages back to the filesystem. Returns bytes recovered.

    A no-op unless the database is in incremental mode, which is what
    ensure_incremental_vacuum arranges.

    Run through `executescript` rather than the usual `execute`, and that is
    not a stylistic choice. `PRAGMA incremental_vacuum` is a statement that
    frees one page per step, and Python's sqlite3 steps a row-less statement
    exactly once — so `execute("PRAGMA incremental_vacuum(100000)")` returns
    successfully having freed a single page. Measured against a database with
    3,609 pages waiting:

        execute only          3609 -> 3608
        execute + fetchall    3609 -> 3608
        executescript         3609 ->    0

    Nothing raises in the first two. It just quietly does nothing, which is
    the same shape as the bug this whole module exists to fix.
    """
    before = db_bytes()
    raw = engine.raw_connection()
    try:
        raw.driver_connection.executescript(
            f"PRAGMA incremental_vacuum({pages or VACUUM_PAGES});")
    except Exception as exc:
        log.warning("incremental vacuum failed: %s", exc)
        return 0
    finally:
        raw.close()
    # Fold the WAL in before measuring. Without this the freed pages are still
    # sitting in it and the figure reported is a fraction of the truth — 1.5 MB
    # for a pass that actually took the file from 21 MB to 4.
    checkpoint()
    return max(0, before - db_bytes())


def checkpoint() -> None:
    """Fold the WAL back into the database.

    A WAL that never check-points is its own way to fill a volume, and after a
    large prune it is holding most of what was deleted.
    """
    try:
        with _autocommit() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        log.warning("wal checkpoint failed: %s", exc)


# ---------- how fast it is filling, and how long that leaves ----------
#
# Retention could always answer "delete anything older than thirty days". It
# could never answer the question an operator actually has — *how fast is this
# filling up and how long have I got* — and without that the only signal the
# disk was in trouble was the disk being in trouble.
#
# Measured rather than assumed, because the answer is entirely local. A
# synthetic corpus puts one article at about **2.8 KB** once its row, its
# full-text index and the indexes over it are counted, so a thousand articles a
# day is roughly 2.8 MB a day. But how many a day *this* instance takes depends
# on its catalog, and an instance polling five hundred city feeds is not the
# instance polling forty wires. So the rate comes from the archive's own
# history.

# One sample an hour is plenty to see a trend and cheap enough to ignore.
SAMPLE_EVERY_SECONDS = float(os.environ.get("NEWS_STORAGE_SAMPLE_EVERY_S", "3600"))
# Long enough that a quiet weekend does not read as a collapse in the rate, and
# short enough to notice a catalog that doubled on Tuesday.
SAMPLE_KEEP_DAYS = float(os.environ.get("NEWS_STORAGE_SAMPLE_DAYS", "14"))
# The rate is refused below this much history. Two samples an hour apart can
# say anything — a checkpoint landing between them looks like a doubling — and
# a wrong rate drives the trimming below.
MIN_SPAN_HOURS = float(os.environ.get("NEWS_STORAGE_MIN_SPAN_H", "6"))


def sample(db) -> dict:
    """Record one measurement of the archive, and drop the stale ones."""
    from datetime import timedelta

    from sqlalchemy import delete as _delete
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from .models import Article, StorageSample, utcnow

    info = disk()
    row = StorageSample(
        db_bytes=db_bytes(),
        free_bytes=int(info.get("free_bytes") or 0) if info.get("ok") else 0,
        articles=int(db.scalar(_select(_func.count(Article.id))) or 0))
    db.add(row)
    db.execute(_delete(StorageSample).where(
        StorageSample.at < utcnow() - timedelta(days=SAMPLE_KEEP_DAYS)))
    db.commit()
    return {"db_bytes": row.db_bytes, "articles": row.articles}


def growth(db) -> dict:
    """How fast the archive is growing, from its own recorded history.

    The oldest and newest samples rather than a fit through all of them: this
    is a number an operator reads to decide whether to buy more disk, and a
    straight line between two real measurements is both easier to defend and
    harder to get subtly wrong than a regression nobody will check.

    Everything is `None` until there is enough history to mean anything, and
    the caller is expected to say "not yet" rather than print a zero.
    """
    from sqlalchemy import select as _select

    from .models import StorageSample

    rows = list(db.scalars(_select(StorageSample).order_by(StorageSample.at)))
    if len(rows) < 2:
        return {"ready": False, "samples": len(rows),
                "reason": "not enough history yet — this needs a few hours"}
    first, last = rows[0], rows[-1]
    hours = (last.at - first.at).total_seconds() / 3600.0
    if hours < MIN_SPAN_HOURS:
        return {"ready": False, "samples": len(rows),
                "reason": f"only {hours:.1f}h of history — needs {MIN_SPAN_HOURS:.0f}h"}

    per_day = (last.db_bytes - first.db_bytes) / hours * 24.0
    articles_per_day = (last.articles - first.articles) / hours * 24.0
    info = disk()
    # Days until the archive reaches its ceiling at the present rate. Only
    # meaningful while it is actually growing: a shrinking archive has no
    # deadline, and dividing by a negative rate would invent one.
    days_left = None
    if per_day > 0:
        headroom = max(0, db_ceiling() - last.db_bytes)
        days_left = round(headroom / per_day, 1)
    return {
        "ready": True,
        "samples": len(rows),
        "span_hours": round(hours, 1),
        "bytes_per_day": int(per_day),
        "articles_per_day": int(articles_per_day),
        # The one number that makes the other two concrete. Measured, not the
        # 2.8 KB a synthetic corpus gives — a real archive's mix of wire copy
        # and full article text is its own.
        "bytes_per_article": (int((last.db_bytes - first.db_bytes)
                                  / (last.articles - first.articles))
                              if last.articles > first.articles else None),
        "days_to_ceiling": days_left,
        "db_bytes": last.db_bytes,
        "free_bytes": info.get("free_bytes") if info.get("ok") else None,
    }
