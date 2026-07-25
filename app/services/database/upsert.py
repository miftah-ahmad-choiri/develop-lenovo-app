"""
upsert.py — Incremental DB updater for daily-upload Excel files.

Triggered only when the user explicitly clicks the Upsert button;
never called automatically on upload.

Public API
----------
dispatch_upsert(category_key, filepath, conn) → int
    Route a validated upload file to the correct upsert function.
    Returns the number of rows processed.  Returns 0 for category
    keys that have no DB table (OPENORDER, PARTONHOLD, etc.).

Individual functions (also importable for testing):
    upsert_wo_summary_and_details(df, conn) → int
    upsert_wo_product_from_msd(df, conn)    → int
    upsert_wo_product_from_shipment(df, conn) → int

Filtering rule (SOID / SHIPMENT)
---------------------------------
Only rows whose work_order_id already exists in wo_summary are written
to wo_product_detail.  Rows referencing an unknown work_order_id are
silently dropped so the FK constraint is never violated and orphan data
is never stored.
"""

from __future__ import annotations

import sqlite3
import pandas as pd

# Re-use the same helper functions from seed.py
from app.services.database.seed import _to_iso, _safe_int, _safe_str, _build_soid


# ── Category keys that map to DB tables ──────────────────────────────────────
# Any key NOT in this set is silently skipped (no DB tables for it yet).
_DB_CATEGORIES = {"WOID", "SOID", "SHIPMENT"}


# ── upsert helpers ────────────────────────────────────────────────────────────

def upsert_wo_summary_and_details(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """
    Upsert rows from a WOID upload (Work Order Advanced Find View) into both
    wo_summary and wo_details tables.

    The WOID upload file contains columns for both tables in one sheet.
    Uses INSERT OR REPLACE so existing rows are fully overwritten with fresh data.

    Returns the number of WO rows processed.
    """
    summary_sql = """
        INSERT OR REPLACE INTO wo_summary (
            work_order_id, serial_number, created_on,
            committed_delivery_date, actual_committed_onsite_date,
            case_desc, work_order_type, contact_name,
            customer, work_order_status, case_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    details_sql = """
        INSERT OR REPLACE INTO wo_details (
            work_order_id, serial_number, case_number,
            product_id_mtm, release_date, original_committed_onsite_date,
            customer_defer_date, completion_date, closing_date,
            premier_service, order_type, work_order_priority,
            city, company_name, address, mobile_phone, primary_email,
            labor_vendor_related, technician_id, closing_code,
            repeat_repair, repeat_repair_reason, wo_cancellation_reason
        ) VALUES (
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?
        )
    """

    summary_rows: list[tuple] = []
    details_rows: list[tuple] = []

    for _, r in df.iterrows():
        wo_id = _safe_int(r.get("Work Order ID"))
        if wo_id is None:
            continue

        summary_rows.append((
            wo_id,
            _safe_str(r.get("Serial Number")),
            _to_iso(r.get("Created On")),
            _to_iso(r.get("Committed Delivery Date")),
            _to_iso(r.get("Actual Committed Onsite Date")),
            _safe_str(r.get("Case")),
            _safe_str(r.get("Work Order Type")),
            _safe_str(r.get(" Contact Name (Contact) (Contact)")),
            _safe_str(r.get("Customer (Labor Vendor Related) (Partner Function)")),
            _safe_str(r.get("Work Order Status")),
            _safe_str(r.get("Case Status (Case) (Case)")),
        ))

        details_rows.append((
            wo_id,
            _safe_str(r.get("Serial Number")),
            _safe_int(r.get("Case Number")),
            _safe_str(r.get("Product ID (MTM)")),
            _to_iso(r.get("Release Date")),
            _to_iso(r.get("Original Committed Onsite Date")),
            _to_iso(r.get("Customer Defer Date")),
            _to_iso(r.get("Completion Date")),
            _to_iso(r.get("Closing Date")),
            _safe_str(r.get("Premier Service")),
            _safe_str(r.get("Order Type")),
            _safe_str(r.get("Work Order Priority")),
            _safe_str(r.get("City")),
            _safe_str(r.get("Company Name")),
            _safe_str(r.get("Address 1 (Contact) (Contact)")),
            _safe_str(r.get("Mobile Phone (Contact) (Contact)")),
            _safe_str(r.get("Primary Email (Contact) (Contact)")),
            _safe_str(r.get("Labor Vendor Related")),
            _safe_str(r.get("Technician ID")),
            _safe_str(r.get("Closing Code")),
            _safe_str(r.get("Repeat Repair")),
            _safe_str(r.get("Repeat Repair Reason")),
            _safe_str(r.get("WO Cancellation Reason")),
        ))

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executemany(summary_sql, summary_rows)
    conn.executemany(details_sql, details_rows)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    return len(summary_rows)


def _load_valid_wo_ids(conn: sqlite3.Connection) -> set[int]:
    """Return the set of work_order_id values currently in wo_summary."""
    rows = conn.execute("SELECT work_order_id FROM wo_summary").fetchall()
    return {r[0] for r in rows}


def upsert_wo_product_from_msd(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """
    Upsert rows from a SOID upload (Work Order Product Advanced Find View) into
    wo_product_detail (MSD columns only).

    Only rows whose work_order_id exists in wo_summary are processed;
    all other rows are silently dropped.

    Two-pass strategy (INSERT OR IGNORE + UPDATE) ensures that existing
    shipment columns on a row are never overwritten by this function.
    """
    valid_wo_ids = _load_valid_wo_ids(conn)

    insert_sql = """
        INSERT OR IGNORE INTO wo_product_detail (
            soid, work_order_id, line_order,
            created_on, product, description,
            acceptance_date, shipment_date, delivery_date, wo_product_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    update_sql = """
        UPDATE wo_product_detail SET
            work_order_id    = ?,
            line_order       = ?,
            created_on       = ?,
            product          = ?,
            description      = ?,
            acceptance_date  = ?,
            shipment_date    = ?,
            delivery_date    = ?,
            wo_product_status= ?
        WHERE soid = ?
    """

    insert_rows: list[tuple] = []
    update_rows: list[tuple] = []

    for _, r in df.iterrows():
        wo_id   = _safe_int(r.get("Work Order"))
        line_no = _safe_int(r.get("Line Order"))
        soid    = _build_soid(wo_id, line_no)
        if soid is None:
            continue
        # Drop rows whose work_order_id is not in wo_summary
        if wo_id not in valid_wo_ids:
            continue

        msd_vals = (
            wo_id,
            line_no,
            _to_iso(r.get("Created On")),
            _safe_str(r.get("Product")),
            _safe_str(r.get("Description")),
            _to_iso(r.get("Acceptance Date")),
            _to_iso(r.get("Shipment Date")),
            _to_iso(r.get("Delivery Date")),
            _safe_str(r.get("Work Order Product Status")),
        )
        insert_rows.append((soid,) + msd_vals)
        update_rows.append(msd_vals + (soid,))

    conn.executemany(insert_sql, insert_rows)
    conn.executemany(update_sql, update_rows)
    conn.commit()
    return len(insert_rows)


def upsert_wo_product_from_shipment(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """
    Upsert rows from a SHIPMENT upload (Lenovo Shipment Daily Report) into
    wo_product_detail (shipment columns only).

    Only rows whose work_order_id (SO column) exists in wo_summary are
    processed; all other rows are silently dropped.

    Two-pass strategy:
    1. INSERT OR IGNORE — creates a skeleton row for new SOIDs.
    2. UPDATE — overwrites shipment columns on all matching rows.
    """
    valid_wo_ids = _load_valid_wo_ids(conn)

    insert_sql = """
        INSERT OR IGNORE INTO wo_product_detail (
            soid, work_order_id,
            order_date, ship_pn, ship_pn_desc, return_flag,
            ship_pickup_time, ship_pou_pod_time, awb, sla, target
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    update_sql = """
        UPDATE wo_product_detail SET
            work_order_id     = COALESCE(work_order_id, ?),
            order_date        = ?,
            ship_pn           = ?,
            ship_pn_desc      = ?,
            return_flag       = ?,
            ship_pickup_time  = ?,
            ship_pou_pod_time = ?,
            awb               = ?,
            sla               = ?,
            target            = ?
        WHERE soid = ?
    """

    insert_rows: list[tuple] = []
    update_rows: list[tuple] = []

    for _, r in df.iterrows():
        soid  = _safe_int(r.get("SOID"))
        wo_id = _safe_int(r.get("SO"))
        if soid is None:
            continue
        # Drop rows whose work_order_id is not in wo_summary
        if wo_id not in valid_wo_ids:
            continue

        shipment_vals = (
            _to_iso(r.get("Order Date")),
            _safe_str(r.get("Ship PN")),
            _safe_str(r.get("Ship PN Desc")),
            _safe_str(r.get("Return Flag")),
            _to_iso(r.get("Ship PickUp Time")),
            _to_iso(r.get("Ship POU POD Time")),
            _safe_str(r.get("AWB")),
            _safe_str(r.get("SLA")),
            _to_iso(r.get("Target")),
        )
        insert_rows.append((soid, wo_id) + shipment_vals)
        update_rows.append((wo_id,) + shipment_vals + (soid,))

    conn.executemany(insert_sql, insert_rows)
    conn.executemany(update_sql, update_rows)
    conn.commit()
    return len(insert_rows)


# ── public dispatcher ─────────────────────────────────────────────────────────

def _purge_orphan_product_rows(conn: sqlite3.Connection) -> int:
    """Delete all wo_product_detail rows whose work_order_id is not in wo_summary.

    Returns the number of rows deleted.
    """
    cur = conn.execute(
        """
        DELETE FROM wo_product_detail
        WHERE work_order_id IS NOT NULL
          AND work_order_id NOT IN (SELECT work_order_id FROM wo_summary)
        """
    )
    conn.commit()
    return cur.rowcount


def dispatch_upsert(category_key: str, filepath: str, conn: sqlite3.Connection) -> int:
    """
    Detect the file's sheet, read it into a DataFrame, route to the correct
    upsert function, then purge any wo_product_detail rows whose work_order_id
    no longer exists in wo_summary.

    Parameters
    ----------
    category_key : str
        One of 'WOID', 'SOID', 'SHIPMENT'.  Other keys are silently ignored
        (returns 0) — they have no DB tables yet.
    filepath : str
        Absolute path to the already-saved and verified Excel file.
    conn : sqlite3.Connection
        Open DB connection (caller is responsible for opening/closing).

    Returns
    -------
    int  — number of rows processed by the category upsert (0 when category
           has no DB table).  Orphan rows deleted are logged separately.

    Raises
    ------
    Exception — any pandas / sqlite3 exception propagates to the caller so
                the upload route can flash an error rather than silently fail.
    """
    key = category_key.upper()
    if key not in _DB_CATEGORIES:
        return 0

    from app.services.upload.upload_verification import verify_uploaded_file

    # Re-use the verification result to get the correct sheet_name cheaply
    result = verify_uploaded_file(filepath)
    sheet_name: str = result.get("sheet_name", "")
    ext = filepath.rsplit(".", 1)[-1].lower()

    if ext == "csv":
        df = pd.read_csv(filepath)
    elif sheet_name:
        df = pd.read_excel(filepath, sheet_name=sheet_name)
    else:
        df = pd.read_excel(filepath)

    if key == "WOID":
        n_rows = upsert_wo_summary_and_details(df, conn)
    elif key == "SOID":
        n_rows = upsert_wo_product_from_msd(df, conn)
    elif key == "SHIPMENT":
        n_rows = upsert_wo_product_from_shipment(df, conn)
    else:
        return 0  # unreachable, but satisfies type checkers

    # After every upsert, purge wo_product_detail rows that reference a
    # work_order_id which is no longer present in wo_summary.
    _purge_orphan_product_rows(conn)

    return n_rows
