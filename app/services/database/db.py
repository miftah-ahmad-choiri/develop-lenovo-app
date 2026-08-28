"""
db.py — SQLite connection helper.

Usage inside a Flask request or CLI context:

    from app.services.database.db import get_db, close_db

    conn = get_db()
    rows = conn.execute("SELECT * FROM wo_summary").fetchall()

The connection is stored on Flask's ``g`` object so it is reused within
the same request/context and closed automatically when the context tears
down (via ``close_db``).

Outside a request context (e.g. seed script, tests, background threads)
use ``open_db(db_path)`` instead — it returns a plain connection that the
caller is responsible for closing.
"""

import sqlite3
from flask import g, current_app

# Pragmas applied to every connection opened by this module.
# WAL  — allows concurrent readers alongside a single writer; eliminates
#         "database is locked" errors caused by overlapping connections.
# FK   — enforce foreign-key constraints at the SQLite level.
_SETUP_SQL = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys = ON",
]


def open_db(db_path: str, timeout: int = 30) -> sqlite3.Connection:
    """Open a standalone SQLite connection suitable for background threads
    and non-request contexts (scripts, upsert workers, meta-cache helpers).

    The caller is responsible for closing the returned connection.

    Parameters
    ----------
    db_path : str
        Absolute path to the SQLite database file.
    timeout : int
        Seconds to wait for a lock before raising OperationalError.
        Default 30 s — generous enough for large upserts under concurrent load.
    """
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for sql in _SETUP_SQL:
        conn.execute(sql)
    return conn


def get_db() -> sqlite3.Connection:
    """Return the SQLite connection for the current application context.

    Opens a new connection on first call within the context and caches it
    on ``g``.  Rows are returned as ``sqlite3.Row`` objects (subscriptable
    by column name).
    """
    if "db" not in g:
        db_path: str = current_app.config["DATABASE_PATH"]
        conn = open_db(db_path, timeout=30)
        g.db = conn
    return g.db


def close_db(e=None) -> None:  # noqa: ANN001
    """Close the SQLite connection at the end of the application context."""
    conn: sqlite3.Connection | None = g.pop("db", None)
    if conn is not None:
        conn.close()
