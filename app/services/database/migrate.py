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
        _migrate_create_asp_details(conn)
        _migrate_create_admin_users(conn)
        _migrate_create_asp_users(conn)
        _migrate_asp_users_drop_tech_id(conn)
        _migrate_create_asp_pw_change_requests(conn)
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


def _migrate_create_admin_users(conn: sqlite3.Connection) -> None:
    """Create the admin_users table and migrate asp000 out of asp_details.

    Steps (all idempotent):
    1. Create admin_users if it does not exist.
    2. If asp000 is still present in asp_details, copy its credentials into
       admin_users and then delete it from asp_details — so the separation
       happens automatically on the first startup after this migration ships.
    """
    # 1. Create table (schema.sql handles IF NOT EXISTS on fresh DBs;
    #    this handles existing DBs that were created before the table existed)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT UNIQUE NOT NULL,
            password        TEXT,
            full_name       TEXT,
            email           TEXT,
            role            TEXT DEFAULT 'admin',
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_admin_users_username
            ON admin_users(username);
        """
    )
    conn.commit()

    # 2. Move asp000 from asp_details → admin_users (once)
    asp000 = conn.execute(
        "SELECT username, password, service_provider FROM asp_details WHERE username = 'asp000'"
    ).fetchone()

    if asp000 is not None:
        # INSERT OR IGNORE so re-running is safe if admin_users already has the row
        conn.execute(
            """
            INSERT OR IGNORE INTO admin_users (username, password, full_name, role, is_active)
            VALUES (?, ?, ?, 'admin', 1)
            """,
            (asp000[0], asp000[1], asp000[2]),
        )
        conn.execute("DELETE FROM asp_details WHERE username = 'asp000'")
        conn.commit()


def _migrate_create_asp_details(conn: sqlite3.Connection) -> None:
    """Create the asp_details table if it does not already exist.

    The DDL in schema.sql uses CREATE TABLE IF NOT EXISTS, so this function
    is a no-op on databases that already have the table.  It exists to handle
    databases created before the asp_details table was added to schema.sql.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='asp_details'"
    ).fetchone()
    if row is not None:
        return  # table already exists

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS asp_details (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            username                TEXT UNIQUE NOT NULL,
            password                TEXT,
            vendor_code             TEXT,
            service_provider        TEXT,
            parent_group            TEXT,
            labor_vendor_related    TEXT,
            customer_partner        TEXT,
            store_name              TEXT,
            kota                    TEXT,
            address                 TEXT,
            lat_long                TEXT,
            link_map                TEXT,
            phone_number            TEXT,
            island                  TEXT,
            working_hours           TEXT,
            operational_status      TEXT,
            future_status           TEXT,
            operation_support       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_asp_details_username
            ON asp_details(username);
        """
    )
    conn.commit()


def _migrate_create_asp_users(conn: sqlite3.Connection) -> None:
    """Create the asp_users table if it does not already exist.

    Each row represents a technician/staff account that belongs to one ASP.
    The asp_username column is a FK → asp_details.username.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS asp_users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            asp_username    TEXT NOT NULL
                                REFERENCES asp_details(username)
                                ON UPDATE CASCADE
                                ON DELETE CASCADE,
            full_name       TEXT NOT NULL,
            email           TEXT NOT NULL,
            password        TEXT NOT NULL,
            phone_number    TEXT,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_asp_users_asp_username
            ON asp_users(asp_username);
        """
    )
    conn.commit()


def _migrate_asp_users_drop_tech_id(conn: sqlite3.Connection) -> None:
    """Drop the tech_id column from asp_users if it still exists.

    SQLite does not support DROP COLUMN before 3.35.0, so we use the
    rename-create-copy-drop pattern inside a transaction.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(asp_users)").fetchall()}
    if "tech_id" not in cols:
        return  # already clean

    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        BEGIN;

        ALTER TABLE asp_users RENAME TO _asp_users_old;

        CREATE TABLE asp_users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            asp_username    TEXT NOT NULL
                                REFERENCES asp_details(username)
                                ON UPDATE CASCADE
                                ON DELETE CASCADE,
            full_name       TEXT NOT NULL,
            email           TEXT NOT NULL,
            password        TEXT NOT NULL,
            phone_number    TEXT,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        INSERT INTO asp_users
            (id, asp_username, full_name, email, password,
             phone_number, is_active, created_at, updated_at)
        SELECT
            id, asp_username, full_name, email, password,
            phone_number, is_active, created_at, updated_at
        FROM _asp_users_old;

        DROP TABLE _asp_users_old;

        CREATE INDEX IF NOT EXISTS idx_asp_users_asp_username
            ON asp_users(asp_username);

        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )
    conn.commit()


def _migrate_create_asp_pw_change_requests(conn: sqlite3.Connection) -> None:
    """Create the asp_pw_change_requests table if it does not already exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS asp_pw_change_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            asp_username    TEXT NOT NULL,
            requested_at    TEXT DEFAULT (datetime('now')),
            status          TEXT DEFAULT 'pending',
            reviewed_by     TEXT,
            reviewed_at     TEXT,
            new_password    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_asp_pw_req_username
            ON asp_pw_change_requests(asp_username);
        CREATE INDEX IF NOT EXISTS idx_asp_pw_req_status
            ON asp_pw_change_requests(status);
        """
    )
    conn.commit()
