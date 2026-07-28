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
_DB_CATEGORIES = {"WOID", "SOID", "SHIPMENT", "PARTONHOLD", "GTAAP"}


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
            product_id_mtm, product_description, release_date, original_committed_onsite_date,
            customer_defer_date, completion_date, closing_date,
            premier_service, order_type, work_order_priority,
            city, company_name, address, mobile_phone, primary_email,
            labor_vendor_related, technician_id, closing_code,
            repeat_repair, repeat_repair_reason, wo_cancellation_reason
        ) VALUES (
            ?, ?, ?,
            ?, ?, ?, ?,
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
            _safe_str(r.get("Product Description")),
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


def upsert_eta_parthold_from_backlog(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """
    Update the ``eta_parthold_backlog`` column on wo_product_detail rows that
    are On Hold - Part Hold, using the SO ETA column from the Backlog Report File.

    Match key: Backlog ``SOID`` → wo_product_detail ``soid``
    Filter:    wo_product_detail.wo_product_status = 'On Hold - Part Hold'
    Write rule:
        - DB eta_parthold_backlog IS NULL  → fill with Excel SO ETA
        - DB eta_parthold_backlog IS NOT NULL but Excel SO ETA is strictly newer → overwrite

    Note: the ``target`` column (SLA deadline from Shipment file) is never touched.

    Returns the number of rows updated.
    """
    import math as _math

    def _has_val(v) -> bool:
        if v is None:
            return False
        if isinstance(v, float) and _math.isnan(v):
            return False
        s = str(v).strip()
        return s not in ("", "nan", "nat", "none", "null", "NaT")

    # Fetch all part-hold rows from DB keyed by soid
    db_rows = {
        r[0]: r[1]  # soid → eta_parthold_backlog (may be None)
        for r in conn.execute(
            "SELECT soid, eta_parthold_backlog FROM wo_product_detail "
            "WHERE LOWER(COALESCE(wo_product_status, '')) = 'on hold - part hold'"
        ).fetchall()
    }

    update_sql = "UPDATE wo_product_detail SET eta_parthold_backlog = ? WHERE soid = ?"
    updates: list[tuple] = []

    for _, r in df.iterrows():
        soid = _safe_int(r.get("SOID"))
        if soid is None or soid not in db_rows:
            continue

        eta_val = r.get("SO ETA")
        if not _has_val(eta_val):
            continue

        eta_iso = _to_iso(eta_val)
        if eta_iso is None:
            continue

        db_eta = db_rows[soid]

        if db_eta is None:
            # Empty — always fill
            updates.append((eta_iso, soid))
        else:
            # Only overwrite if Excel SO ETA is strictly newer
            try:
                from datetime import datetime as _dt
                db_dt  = _dt.fromisoformat(db_eta[:10])
                eta_dt = _dt.fromisoformat(eta_iso[:10])
                if eta_dt > db_dt:
                    updates.append((eta_iso, soid))
            except (ValueError, TypeError):
                pass  # unparseable dates — skip

    conn.executemany(update_sql, updates)
    conn.commit()
    return len(updates)


def upsert_dc_from_gtaap(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """
    Update the ``dc_number`` column on wo_product_detail using the GTAAP Report.

    Two-pass write strategy
    -----------------------
    Pass 1 — fill real DC# values from Excel:
        Eligible rows: db dc_number IS NULL  OR  db dc_number = '0'
        Source: Excel DC# column (non-empty rows only)
        Action: write the normalised DC# string ("17731" not "17731.0")

    Pass 2 — backfill '0' sentinel for no-return rows:
        Eligible rows: db dc_number IS NULL after pass 1,
                       AND the GTAAP row has Return Flag = 'No' / 'N' / 'NO'
        Action: write '0' to signal "no DC# expected (non-returnable part)"

    A dc_number that already holds a real value (anything other than NULL/'0')
    is never overwritten.

    Returns the total number of rows updated across both passes.
    """
    import math as _math

    def _has_dc(v) -> bool:
        if v is None:
            return False
        if isinstance(v, float) and _math.isnan(v):
            return False
        s = str(v).strip()
        return s not in ("", "nan", "nat", "none", "null", "NaT")

    def _dc_eligible(current: str | None) -> bool:
        """True when the current DB value should be overwritten."""
        return current is None or current.strip() == "0"

    def _normalise_dc(dc_val) -> str:
        """Normalise whole-number floats: 17731.0 → '17731'."""
        try:
            f = float(dc_val)
            if f == int(f):
                return str(int(f))
        except (ValueError, TypeError):
            pass
        return str(dc_val).strip()

    # Fetch current dc_number state for all rows, keyed by soid
    db_dc = {
        r[0]: r[1]  # soid → dc_number (may be None)
        for r in conn.execute(
            "SELECT soid, dc_number FROM wo_product_detail"
        ).fetchall()
    }

    update_sql = "UPDATE wo_product_detail SET dc_number = ? WHERE soid = ?"
    updates: list[tuple] = []

    # ── Pass 1: write real DC# values from Excel ─────────────────────────────
    for _, r in df.iterrows():
        soid = _safe_int(r.get("SOID"))
        if soid is None or soid not in db_dc:
            continue
        if not _dc_eligible(db_dc[soid]):
            continue

        dc_val = r.get("DC#")
        if not _has_dc(dc_val):
            continue

        dc_str = _normalise_dc(dc_val)
        updates.append((dc_str, soid))
        db_dc[soid] = dc_str  # keep in-memory state current for pass 2

    # ── Pass 2: backfill '0' for non-returnable rows still empty ─────────────
    _no_return = {"n", "no"}
    for _, r in df.iterrows():
        soid = _safe_int(r.get("SOID"))
        if soid is None or soid not in db_dc:
            continue
        # Only rows that are still NULL after pass 1
        if db_dc[soid] is not None:
            continue
        return_flag = str(r.get("Return Flag") or "").strip().lower()
        if return_flag in _no_return:
            updates.append(("0", soid))
            db_dc[soid] = "0"

    conn.executemany(update_sql, updates)
    conn.commit()
    return len(updates)


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

    import io as _io
    from app.services.upload.upload_verification import verify_uploaded_file

    # Re-use the verification result to get the correct sheet_name cheaply
    result = verify_uploaded_file(filepath)
    sheet_name: str = result.get("sheet_name", "")
    ext = filepath.rsplit(".", 1)[-1].lower()

    # Read into memory first so openpyxl's handle (opened by verify_uploaded_file)
    # does not block the subsequent pd.read_excel call on Windows.
    with open(filepath, "rb") as _fh:
        _file_bytes = _io.BytesIO(_fh.read())

    if ext == "csv":
        df = pd.read_csv(_file_bytes)
    elif sheet_name:
        df = pd.read_excel(_file_bytes, sheet_name=sheet_name)
    else:
        df = pd.read_excel(_file_bytes)

    if key == "WOID":
        n_rows = upsert_wo_summary_and_details(df, conn)
    elif key == "SOID":
        n_rows = upsert_wo_product_from_msd(df, conn)
    elif key == "SHIPMENT":
        n_rows = upsert_wo_product_from_shipment(df, conn)
    elif key == "PARTONHOLD":
        n_rows = upsert_eta_parthold_from_backlog(df, conn)
        return n_rows  # no orphan purge needed for this category
    elif key == "GTAAP":
        n_rows = upsert_dc_from_gtaap(df, conn)
        return n_rows  # no orphan purge needed for this category
    else:
        return 0  # unreachable, but satisfies type checkers

    # After every upsert, purge wo_product_detail rows that reference a
    # work_order_id which is no longer present in wo_summary.
    _purge_orphan_product_rows(conn)

    return n_rows
