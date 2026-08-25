"""
db.py — SQLite connection helper.

Usage inside a Flask request or CLI context:

    from app.services.database.db import get_db, close_db

    conn = get_db()
    rows = conn.execute("SELECT * FROM wo_summary").fetchall()

The connection is stored on Flask's ``g`` object so it is reused within
the same request/context and closed automatically when the context tears
down (via ``close_db``).

Outside a request context (e.g. seed script, tests) you can also call
``get_db()`` directly — just make sure you are inside an application
context (``with app.app_context()``).
"""

import sqlite3
from flask import g, current_app


def get_db() -> sqlite3.Connection:
    """Return the SQLite connection for the current application context.

    Opens a new connection on first call within the context and caches it
    on ``g``.  Rows are returned as ``sqlite3.Row`` objects (subscriptable
    by column name).
    """
    if "db" not in g:
        db_path: str = current_app.config["DATABASE_PATH"]
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers alongside a writer — prevents
        # "database is locked" 500s when the background MSD/sync thread is
        # mid-write while a request tries to read.
        conn.execute("PRAGMA journal_mode=WAL")
        # Enforce foreign-key constraints
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(e=None) -> None:  # noqa: ANN001
    """Close the SQLite connection at the end of the application context."""
    conn: sqlite3.Connection | None = g.pop("db", None)
    if conn is not None:
        conn.close()
