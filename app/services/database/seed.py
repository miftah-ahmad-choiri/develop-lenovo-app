"""
seed.py — One-time seed loader: reads the four source-db Excel files and
populates the three SQLite tables (wo_summary, wo_details, wo_product_detail).

Run once via:

    flask seed-db

The function is fully idempotent — every insert uses INSERT OR IGNORE on the
primary key, so re-running it against a populated database is safe.

Source files expected under files/source-db/:
    Work Order Summary.xlsx           → wo_summary
    Work Order Details.xlsx           → wo_details
    Work Order Product Details.xlsx   → wo_product_detail  (MSD columns)
    Lenovo Shipment Daily Report.xlsx → wo_product_detail  (Shipment columns)

SOID key construction (MSD side):
    soid = int(str(work_order_id) + str(line_order))
    e.g. work_order_id=4020720183, line_order=20  →  soid=402072018320

SOID key on Shipment side:
    Native "SOID" column — already the combined integer.
    "SO" column = work_order_id.
"""

import os
import sqlite3
import pandas as pd
from flask import Flask


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_iso(val) -> str | None:
    """Convert a cell value to an ISO datetime string or None."""
    if val is None:
        return None
    if isinstance(val, float) and val != val:   # NaN
        return None
    try:
        if pd.isnull(val):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip()
    return s if s else None


def _safe_int(val) -> int | None:
    """Cast a value to int, handling float representation (e.g. 4020720183.0)."""
    if val is None:
        return None
    try:
        if isinstance(val, float) and val != val:
            return None
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def _safe_str(val) -> str | None:
    """Return a stripped string or None for blank/NaN values.

    Floats that are whole numbers (e.g. 90209284.0 from Excel) are
    converted to their integer string form (e.g. "90209284").
    """
    if val is None:
        return None
    if isinstance(val, float) and val != val:   # NaN
        return None
    try:
        if pd.isnull(val):
            return None
    except (TypeError, ValueError):
        pass
    # Strip float-integer suffix: "90209284.0" → "90209284"
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    s = str(val).strip()
    # Also handle strings that pandas emitted as "xxxxx.0"
    if s.endswith('.0') and s[:-2].lstrip('-').isdigit():
        s = s[:-2]
    return s if s else None


def _build_soid(work_order_id: int | None, line_order: int | None) -> int | None:
    """Compute SOID by concatenating work_order_id and line_order as strings."""
    if work_order_id is None or line_order is None:
        return None
    return int(str(work_order_id) + str(line_order))


# ── seed functions ────────────────────────────────────────────────────────────

def _seed_wo_summary(conn: sqlite3.Connection, filepath: str) -> int:
    """
    Load Work Order Summary.xlsx (sheet WO_Summary) → wo_summary table.
    Returns the number of rows inserted.
    """
    df = pd.read_excel(filepath, sheet_name="WO_Summary")

    sql = """
        INSERT OR IGNORE INTO wo_summary (
            work_order_id, serial_number, created_on,
            committed_delivery_date, actual_committed_onsite_date,
            case_desc, work_order_type, contact_name,
            customer, work_order_status, case_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = []
    for _, r in df.iterrows():
        wo_id = _safe_int(r.get("Work Order ID"))
        if wo_id is None:
            continue
        rows.append((
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
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def _seed_wo_details(conn: sqlite3.Connection, filepath: str) -> int:
    """
    Load Work Order Details.xlsx (sheet WO_Details) → wo_details table.
    Returns the number of rows inserted.
    """
    df = pd.read_excel(filepath, sheet_name="WO_Details")

    sql = """
        INSERT OR IGNORE INTO wo_details (
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

    rows = []
    for _, r in df.iterrows():
        wo_id = _safe_int(r.get("Work Order ID"))
        if wo_id is None:
            continue
        rows.append((
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

    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def _seed_wo_product_from_msd(conn: sqlite3.Connection, filepath: str) -> int:
    """
    Load Work Order Product Details.xlsx (sheet 'Work Order Product Advanced...')
    → wo_product_detail table (MSD columns).

    SOID is computed as:  int(str(work_order_id) + str(line_order))

    Returns the number of rows inserted.
    """
    # The sheet name is truncated in the file — find it dynamically
    xf = pd.ExcelFile(filepath)
    sheet = next(
        (s for s in xf.sheet_names if s.lower().startswith("work order product")),
        xf.sheet_names[0],
    )
    df = pd.read_excel(filepath, sheet_name=sheet)

    sql = """
        INSERT OR IGNORE INTO wo_product_detail (
            soid, work_order_id, line_order,
            created_on, product, description,
            acceptance_date, shipment_date, delivery_date, wo_product_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = []
    for _, r in df.iterrows():
        wo_id   = _safe_int(r.get("Work Order"))
        line_no = _safe_int(r.get("Line Order"))
        soid    = _build_soid(wo_id, line_no)
        if soid is None:
            continue
        rows.append((
            soid,
            wo_id,
            line_no,
            _to_iso(r.get("Created On")),
            _safe_str(r.get("Product")),
            _safe_str(r.get("Description")),
            _to_iso(r.get("Acceptance Date")),
            _to_iso(r.get("Shipment Date")),
            _to_iso(r.get("Delivery Date")),
            _safe_str(r.get("Work Order Product Status")),
        ))

    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def _seed_wo_product_from_shipment(conn: sqlite3.Connection, filepath: str) -> int:
    """
    Load Lenovo Shipment Daily Report.xlsx (sheet Sheet1)
    → wo_product_detail table (Shipment columns).

    Two-pass strategy:
    1. INSERT OR IGNORE — creates a row for SOIDs not yet in the table
       (MSD file may not have been loaded yet, or this shipment has no MSD row).
    2. UPDATE — fills shipment columns on rows already inserted by the MSD pass.

    Returns the number of rows processed.
    """
    df = pd.read_excel(filepath, sheet_name="Sheet1")

    insert_sql = """
        INSERT OR IGNORE INTO wo_product_detail (
            soid, work_order_id,
            order_date, ship_pn, ship_pn_desc, return_flag,
            ship_pickup_time, ship_pou_pod_time, awb, sla, target
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    update_sql = """
        UPDATE wo_product_detail SET
            work_order_id    = COALESCE(work_order_id, ?),
            order_date       = ?,
            ship_pn          = ?,
            ship_pn_desc     = ?,
            return_flag      = ?,
            ship_pickup_time = ?,
            ship_pou_pod_time= ?,
            awb              = ?,
            sla              = ?,
            target           = ?
        WHERE soid = ?
    """

    insert_rows = []
    update_rows = []

    for _, r in df.iterrows():
        soid  = _safe_int(r.get("SOID"))
        wo_id = _safe_int(r.get("SO"))
        if soid is None:
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


# ── public entry point ────────────────────────────────────────────────────────

def seed_from_source_db(app: Flask) -> dict[str, int]:
    """
    Seed all three tables from the four source-db Excel files.

    Must be called inside a Flask application context, or pass the app
    object directly (the function creates its own context via app.app_context()).

    Returns a dict with row counts per step::

        {
            "wo_summary":              21825,
            "wo_details":              21825,
            "wo_product_from_msd":     36143,
            "wo_product_from_shipment": 6299,
        }
    """
    source_dir: str = app.config["SOURCE_DB_DIR"]
    db_path:    str = app.config["DATABASE_PATH"]

    files = {
        "summary":  os.path.join(source_dir, "Work Order Summary.xlsx"),
        "details":  os.path.join(source_dir, "Work Order Details.xlsx"),
        "products": os.path.join(source_dir, "Work Order Product Details.xlsx"),
        "shipment": os.path.join(source_dir, "Lenovo Shipment Daily Report.xlsx"),
    }

    # Verify all source files exist before opening the DB
    missing = [k for k, p in files.items() if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(
            f"Missing source-db files: {missing}\n"
            f"Expected directory: {source_dir}"
        )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")   # allow inserting details before summary if needed

    try:
        print("  [1/4] Seeding wo_summary …")
        n_summary = _seed_wo_summary(conn, files["summary"])
        print(f"        {n_summary:,} rows")

        print("  [2/4] Seeding wo_details …")
        n_details = _seed_wo_details(conn, files["details"])
        print(f"        {n_details:,} rows")

        print("  [3/4] Seeding wo_product_detail from MSD product file …")
        n_msd = _seed_wo_product_from_msd(conn, files["products"])
        print(f"        {n_msd:,} rows")

        print("  [4/4] Seeding wo_product_detail from Shipment file …")
        n_ship = _seed_wo_product_from_shipment(conn, files["shipment"])
        print(f"        {n_ship:,} rows processed")

    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()

    return {
        "wo_summary":               n_summary,
        "wo_details":               n_details,
        "wo_product_from_msd":      n_msd,
        "wo_product_from_shipment": n_ship,
    }
