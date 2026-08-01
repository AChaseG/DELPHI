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
