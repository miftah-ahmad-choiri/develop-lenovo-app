"""
migrate.py — Run DDL migrations to create / update the SQLite schema.

Called once from ``create_app()`` on every startup.  The DDL uses
``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS``, so it
is safe to run repeatedly and will never overwrite existing data.
"""

import os
import sqlite3
from flask import Flask


def run_migrations(app: Flask) -> None:
    """Execute schema.sql against the configured database.

    Creates the database file if it does not exist yet.
    """
    db_path: str = app.config["DATABASE_PATH"]
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")

    # Ensure the parent directory exists (files/ is tracked but may be absent
    # in a fresh clone that strips empty dirs)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    with open(schema_path, "r", encoding="utf-8") as fh:
        ddl = fh.read()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(ddl)
        conn.commit()
        _migrate_wo_product_detail_cascade(conn)
        _migrate_wo_details_add_product_description(conn)
        _migrate_wo_product_detail_add_eta_parthold(conn)
        _migrate_wo_product_detail_add_dc_number(conn)
    finally:
        conn.close()


def _migrate_wo_product_detail_cascade(conn: sqlite3.Connection) -> None:
    """Rebuild wo_product_detail with ON DELETE CASCADE if not already set.

    SQLite does not support ALTER COLUMN, so we use the recommended
    rename-create-copy-drop pattern inside a transaction.  This is a
    no-op when the table already carries the cascade constraint.
    """
    # Inspect the current CREATE TABLE statement stored in sqlite_master.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='wo_product_detail'"
    ).fetchone()

    # Table does not exist yet (fresh DB) or cascade is already present — nothing to do.
    if row is None or "ON DELETE CASCADE" in row[0].upper():
        return

    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;

        BEGIN;

        -- Step 1: rename the existing table
        ALTER TABLE wo_product_detail RENAME TO _wo_product_detail_old;

        -- Step 2: create the table with the cascade constraint
        CREATE TABLE wo_product_detail (
            soid                INTEGER PRIMARY KEY,
            work_order_id       INTEGER
                                    REFERENCES wo_summary(work_order_id)
                                    ON DELETE CASCADE,
            line_order          INTEGER,
            created_on          TEXT,
            product             TEXT,
            description         TEXT,
            acceptance_date     TEXT,
            shipment_date       TEXT,
            delivery_date       TEXT,
            wo_product_status   TEXT,
            order_date          TEXT,
            ship_pn             TEXT,
            ship_pn_desc        TEXT,
            return_flag         TEXT,
            ship_pickup_time    TEXT,
            ship_pou_pod_time   TEXT,
            awb                 TEXT,
            sla                 TEXT,
            target              TEXT
        );

        -- Step 3: copy all rows (orphaned rows whose work_order_id is not in
        --         wo_summary are dropped here via the WHERE clause so the FK
        --         insert does not fail)
        INSERT INTO wo_product_detail
        SELECT * FROM _wo_product_detail_old
        WHERE work_order_id IS NULL
           OR work_order_id IN (SELECT work_order_id FROM wo_summary);

        -- Step 4: drop old table
        DROP TABLE _wo_product_detail_old;

        COMMIT;

        PRAGMA foreign_keys = ON;
        """
    )


def _migrate_wo_product_detail_add_eta_parthold(conn: sqlite3.Connection) -> None:
    """Add eta_parthold_backlog column to wo_product_detail if it does not exist.

    SQLite supports ADD COLUMN without rebuilding the table — cheap and
    safe to run on every startup.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(wo_product_detail)").fetchall()
    }
    if "eta_parthold_backlog" not in existing:
        conn.execute(
            "ALTER TABLE wo_product_detail ADD COLUMN eta_parthold_backlog TEXT"
        )
        conn.commit()


def _migrate_wo_details_add_product_description(conn: sqlite3.Connection) -> None:
    """Add product_description column to wo_details if it does not already exist.

    SQLite supports ADD COLUMN without rebuilding the table, so this is cheap
    and safe to run on every startup.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(wo_details)").fetchall()
    }
    if "product_description" not in existing:
        conn.execute(
            "ALTER TABLE wo_details ADD COLUMN product_description TEXT"
        )
        conn.commit()


def _migrate_wo_product_detail_add_dc_number(conn: sqlite3.Connection) -> None:
    """Add dc_number column to wo_product_detail if it does not exist.

    Populated by the GTAAP Report upsert — maps SOID → DC# from the
    Resolv GTAAP export file.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(wo_product_detail)").fetchall()
    }
    if "dc_number" not in existing:
        conn.execute(
            "ALTER TABLE wo_product_detail ADD COLUMN dc_number TEXT"
        )
        conn.commit()
