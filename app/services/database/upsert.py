"""
upsert.py — Incremental DB updater for daily-upload Excel files.

Triggered only when the user explicitly clicks the Upsert button;
never called automatically on upload.

Public API
----------
dispatch_upsert(category_key, filepath, conn) → int | tuple[int, int, int] | tuple[int, int]
    Route a validated upload file to the correct upsert function.
    Returns the number of rows processed (int) for most categories.
    For WOID, returns (n_new_wo, n_updated_wo, n_new_asp_users) as a tuple.
    For GTAAP, returns (n_new_dc, n_new_status) as a tuple.
    Returns 0 for category keys that have no DB table.

Individual functions (also importable for testing):
    upsert_wo_summary_and_details(df, conn) → tuple[int, int, int]
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
_DB_CATEGORIES = {"WOID", "SOID", "SHIPMENT", "PARTONHOLD", "GTAAP", "UNRETURN"}


# ── upsert helpers ────────────────────────────────────────────────────────────

def upsert_wo_summary_and_details(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """
    Upsert rows from a WOID upload (Work Order Advanced Find View) into both
    wo_summary and wo_details tables.

    Smart-diff filter — a row is written only when ANY of these conditions hold:
      1. work_order_id does not exist in wo_summary or wo_details (NEW row).
      2. Any date column in Excel is strictly later than the value already in
         the DB, or the DB value is NULL and Excel supplies a real value.
      3. Any of the five tracked status columns has a different value in Excel
         vs the DB: Work Order Status, Case Status, Closing Code,
         Repeat Repair Reason, WO Cancellation Reason.

    Rows that pass the filter are written with INSERT OR REPLACE, which
    fully overwrites all columns for that work_order_id.

    Returns the number of WO rows processed.
    """
    import math as _math
    from datetime import datetime as _dt

    # ── DB column maps ────────────────────────────────────────────────────────
    _date_cols = {
        "Created On":                       ("wo_summary", "created_on"),
        "Committed Delivery Date":          ("wo_summary", "committed_delivery_date"),
        "Actual Committed Onsite Date":     ("wo_summary", "actual_committed_onsite_date"),
        "Release Date":                     ("wo_details", "release_date"),
        "Original Committed Onsite Date":   ("wo_details", "original_committed_onsite_date"),
        "Customer Defer Date":              ("wo_details", "customer_defer_date"),
        "Completion Date":                  ("wo_details", "completion_date"),
        "Closing Date":                     ("wo_details", "closing_date"),
    }
    _status_cols = {
        "Work Order Status":          ("wo_summary", "work_order_status"),
        "Case Status (Case) (Case)":  ("wo_summary", "case_status"),
        "Closing Code":               ("wo_details", "closing_code"),
        "Repeat Repair Reason":       ("wo_details", "repeat_repair_reason"),
        "WO Cancellation Reason":     ("wo_details", "wo_cancellation_reason"),
    }

    # ── load current DB state (one pass, keyed by work_order_id) ─────────────
    db_summary = {
        r[0]: {
            "created_on":                   r[1],
            "committed_delivery_date":      r[2],
            "actual_committed_onsite_date": r[3],
            "work_order_status":            r[4],
            "case_status":                  r[5],
        }
        for r in conn.execute(
            "SELECT work_order_id, created_on, committed_delivery_date, "
            "actual_committed_onsite_date, work_order_status, case_status "
            "FROM wo_summary"
        ).fetchall()
    }
    db_details = {
        r[0]: {
            "release_date":                   r[1],
            "original_committed_onsite_date": r[2],
            "customer_defer_date":            r[3],
            "completion_date":                r[4],
            "closing_date":                   r[5],
            "closing_code":                   r[6],
            "repeat_repair_reason":           r[7],
            "wo_cancellation_reason":         r[8],
        }
        for r in conn.execute(
            "SELECT work_order_id, release_date, original_committed_onsite_date, "
            "customer_defer_date, completion_date, closing_date, "
            "closing_code, repeat_repair_reason, wo_cancellation_reason "
            "FROM wo_details"
        ).fetchall()
    }

    # ── diff helpers ──────────────────────────────────────────────────────────
    def _has_val(v) -> bool:
        if v is None:
            return False
        if isinstance(v, float) and _math.isnan(v):
            return False
        s = str(v).strip()
        return s not in ("", "nan", "nat", "none", "null", "NaT")

    def _date_iso(v):
        """Return YYYY-MM-DD string or None."""
        if not _has_val(v):
            return None
        raw = _to_iso(v)
        return raw[:10] if raw else None

    def _date_newer(excel_val, db_str) -> bool:
        ex = _date_iso(excel_val)
        if ex is None:
            return False
        if db_str is None:
            return True
        try:
            return _dt.fromisoformat(ex) > _dt.fromisoformat(db_str[:10])
        except (ValueError, TypeError):
            return False

    def _status_changed(excel_val, db_val) -> bool:
        ex_s = str(excel_val).strip() if _has_val(excel_val) else ""
        db_s = str(db_val).strip()    if db_val is not None else ""
        return ex_s != db_s

    def _qualifies(wo_id: int, row) -> bool:
        """Return True when this Excel row should be upserted."""
        # Rule 1 — brand new WO
        if wo_id not in db_summary or wo_id not in db_details:
            return True
        s_row = db_summary[wo_id]
        d_row = db_details[wo_id]
        # Rule 2 — any date column is newer in Excel
        for excel_col, (tbl, db_col) in _date_cols.items():
            db_val = (s_row if tbl == "wo_summary" else d_row).get(db_col)
            if _date_newer(row.get(excel_col), db_val):
                return True
        # Rule 3 — any status column changed
        for excel_col, (tbl, db_col) in _status_cols.items():
            db_val = (s_row if tbl == "wo_summary" else d_row).get(db_col)
            if _status_changed(row.get(excel_col), db_val):
                return True
        return False

    # ── SQL statements ────────────────────────────────────────────────────────
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
            labor_vendor_related, tech_id, closing_code,
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
    new_wo_ids:   set[int]    = set()   # WO IDs brand-new (not in DB before this upsert)

    for _, r in df.iterrows():
        wo_id = _safe_int(r.get("Work Order ID"))
        if wo_id is None:
            continue
        # Smart-diff gate — skip rows with no qualifying change
        if not _qualifies(wo_id, r):
            continue
        # Track whether this is a brand-new WO for stats reporting
        if wo_id not in db_summary or wo_id not in db_details:
            new_wo_ids.add(wo_id)

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
            _safe_str(r.get("LEAP ID (Technician ID) (Contact)")),
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

    # ── Sync asp_users: insert new technicians found in this file ────────────
    # For every row that has a LEAP ID, collect (leap_id, full_name, vendor).
    # Only insert rows whose leap_id is not already in asp_users.
    # Never updates existing rows — this is insert-only for new technicians.
    n_new_users = 0
    leap_col   = "LEAP ID (Technician ID) (Contact)"
    tname_col  = "Technician ID"
    vendor_col = "Labor Vendor Related"
    if leap_col in df.columns:
        # Build map: leap_id → (vendor, leap_id, full_name) — first occurrence wins
        new_users: dict[str, tuple] = {}
        for _, r in df.iterrows():
            leap = _safe_str(r.get(leap_col))
            if not leap:
                continue
            name   = _safe_str(r.get(tname_col))
            vendor = _safe_str(r.get(vendor_col))
            if leap not in new_users and name:
                new_users[leap] = (vendor, leap, name)

        if new_users:
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT tech_id FROM asp_users WHERE tech_id IS NOT NULL"
                ).fetchall()
            }
            to_insert = [v for k, v in new_users.items() if k not in existing]
            if to_insert:
                conn.executemany(
                    "INSERT INTO asp_users"
                    " (labor_vendor_related, tech_id, full_name, email, password)"
                    " VALUES (?, ?, ?, '', '')",
                    to_insert,
                )
                conn.commit()
                n_new_users = len(to_insert)

    n_total   = len(summary_rows)
    n_new_wo  = len(new_wo_ids)
    n_updated = n_total - n_new_wo
    return n_new_wo, n_updated, n_new_users


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


# Status hierarchy for GTAAP return_status.
# A row can only move forward (higher rank) — never backward, never overwrite
# a value of equal or higher rank.
#   PENDING WITH PARTNER       → rank 0  (lowest / open — pickup stage)
#   PENDING FOR DC GENERATION  → rank 1  (DC form being prepared)
#   DC GENERATED               → rank 2  (locked — never overwritten)
# UNKNOWN is intentionally excluded from this hierarchy (rank -1) so it can
# be overwritten by any valid status, including DC GENERATED.
_GTAAP_STATUS_RANK: dict[str, int] = {
    "PENDING WITH PARTNER":      0,
    "PENDING FOR DC GENERATION": 1,
    "DC GENERATED":              2,
}


def _gtaap_status_eligible(db_status: str | None, excel_status: str) -> bool:
    """Return True when *excel_status* is allowed to overwrite *db_status*.

    Rules:
    - DB value of 'DC GENERATED' (rank 2) is always locked.
    - Any other transition is allowed only when the incoming rank is strictly
      higher than the stored rank (forward-only movement).
    - Unknown status strings (not in the hierarchy) are treated as rank -1
      so they can always be overwritten but never used to overwrite anything.

    Hierarchy (lowest → highest):
      PENDING WITH PARTNER (0) → PENDING FOR DC GENERATION (1) → DC GENERATED (2, locked)
    """
    incoming_rank = _GTAAP_STATUS_RANK.get(excel_status, -1)
    if incoming_rank < 0:
        return False  # unrecognised excel value — skip
    current_rank = _GTAAP_STATUS_RANK.get(str(db_status or "").strip(), -1)
    # current_rank -1 means NULL / unknown → always eligible
    return incoming_rank > current_rank


def upsert_dc_from_gtaap(df: pd.DataFrame, conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Update ``dc_number`` and ``return_status`` on wo_product_detail using the GTAAP Report.

    DC# write strategy
    ------------------
    Pass 1 — fill real DC# values from Excel:
        Eligible rows: db dc_number IS NULL
        Source: Excel DC# column (non-empty rows only)
        Action: write the normalised DC# string ("17731" not "17731.0")

    Pass 2 — removed. No-return rows simply keep dc_number = NULL; '0' is no
        longer written as a sentinel since NULL already means "no DC# expected".

    A dc_number that already holds a real value is never overwritten.

    return_status write strategy (hierarchy-enforced)
    -------------------------------------------------
    Written only when the incoming Excel Status is a forward move in the
    hierarchy:  PENDING FOR DC GENERATION (0) → PENDING WITH PARTNER (1)
                                               → DC GENERATED (2, locked).
    A stored 'DC GENERATED' is never overwritten.

    Pass 4 — absent rows:
        Rows with PENDING WITH PARTNER or PENDING FOR DC GENERATION whose
        SOID and work_order_id are both absent from the Excel file are set
        to 'UNKNOWN' (not yet DC GENERATED — dc_number still missing).
        When a subsequent upload writes a real dc_number to such a row,
        Pass 1b / Pass 5a will promote it from UNKNOWN → DC GENERATED.

    Pass 5a — promote (whole-table, runs after all writes):
        Any row with a real dc_number whose return_status is not already
        'DC GENERATED' (including UNKNOWN) is promoted to 'DC GENERATED' —
        catches rows filled by earlier imports.

    Pass 5b — cleanup (whole-table, runs after all writes):
        Any row where dc_number is NULL or empty but return_status is
        'DC GENERATED' is inconsistent; return_status is cleared to NULL.

    Returns
    -------
    (n_new_dc, n_new_status) : tuple[int, int]
        n_new_dc     — number of dc_number values written (Pass 1)
        n_new_status — number of return_status values written (Passes 1b + 3 + 4)
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
        """True when the current DB dc_number value should be overwritten.

        A real incoming value (_has_dc check is done before calling this) is
        always allowed to write — whether the DB slot is empty or already filled.
        NULL/empty incoming values are rejected upstream, so the only thing this
        guard needs to do is return True unconditionally: any real value may land.

        Kept as an explicit function so the intent is clear and future rules can
        be added here without touching the call-site loop.
        """
        return True  # real → any (NULL or real): always allow; NULL→NULL blocked by _has_dc

    def _normalise_dc(dc_val) -> str:
        """Normalise whole-number floats: 17731.0 → '17731'."""
        try:
            f = float(dc_val)
            if f == int(f):
                return str(int(f))
        except (ValueError, TypeError):
            pass
        return str(dc_val).strip()

    # Fetch current dc_number, return_status, and work_order_id for all rows
    db_rows = {
        r[0]: {"dc_number": r[1], "return_status": r[2], "work_order_id": r[3]}
        for r in conn.execute(
            "SELECT soid, dc_number, return_status, work_order_id FROM wo_product_detail"
        ).fetchall()
    }
    db_dc = {soid: v["dc_number"] for soid, v in db_rows.items()}

    # SOIDs whose return_status is already at the top of the hierarchy (DC GENERATED,
    # rank 2) are fully blocked — no column in their row may be changed by this upload.
    blocked_soids: set = {
        soid for soid, v in db_rows.items()
        if str(v["return_status"] or "").strip() == "DC GENERATED"
    }

    dc_updates: list[tuple] = []
    status_updates: list[tuple] = []

    # ── Pass 1: write real DC# values from Excel ─────────────────────────────
    for _, r in df.iterrows():
        soid = _safe_int(r.get("SOID"))
        if soid is None or soid not in db_rows:
            continue
        if soid in blocked_soids:          # fully blocked — skip entire row
            continue
        dc_val = r.get("DC#")
        if not _has_dc(dc_val):
            continue

        if not _dc_eligible(db_dc[soid]):
            continue

        dc_str = _normalise_dc(dc_val)
        dc_updates.append((dc_str, soid))
        db_dc[soid] = dc_str  # keep in-memory state current for pass 2

    # ── Pass 1b: promote to DC GENERATED when a real DC# was just written ────
    # Rule: if dc_number is now a real value and return_status is not already
    # 'DC GENERATED', force it to 'DC GENERATED'.
    # This also promotes UNKNOWN → DC GENERATED when a dc_number arrives.
    for dc_str, soid in dc_updates:
        db_status = str(db_rows[soid]["return_status"] or "").strip()
        if db_status != "DC GENERATED":
            status_updates.append(("DC GENERATED", soid))
            db_rows[soid]["return_status"] = "DC GENERATED"  # keep in-memory state current

    # ── Pass 3: write return_status — hierarchy-enforced ─────────────────────
    for _, r in df.iterrows():
        soid = _safe_int(r.get("SOID"))
        if soid is None or soid not in db_rows:
            continue
        if soid in blocked_soids:          # fully blocked — skip entire row
            continue
        excel_status = str(r.get("Status") or "").strip()
        db_status    = db_rows[soid]["return_status"]
        if _gtaap_status_eligible(db_status, excel_status):
            status_updates.append((excel_status, soid))

    # ── Pass 4: absent rows — set to UNKNOWN ─────────────────────────────────
    # DB rows with return_status PENDING FOR DC GENERATION or PENDING WITH PARTNER
    # that have no matching SOID *and* no matching work_order_id in the Excel file
    # are no longer open in GTAAP but we don't have a dc_number yet.
    # Set to 'UNKNOWN' as a placeholder; a subsequent upload that provides a real
    # dc_number will promote UNKNOWN → DC GENERATED via Pass 1b / Pass 5a.
    # Blocked rows (DC GENERATED) are excluded — they never receive UNKNOWN.
    _open_statuses = {"PENDING FOR DC GENERATION", "PENDING WITH PARTNER"}
    excel_soids   = {
        _safe_int(r.get("SOID"))
        for _, r in df.iterrows()
        if _safe_int(r.get("SOID")) is not None
    }
    excel_wo_ids  = {
        _safe_int(r.get("WO#"))
        for _, r in df.iterrows()
        if _safe_int(r.get("WO#")) is not None
    }
    for soid, row_data in db_rows.items():
        if soid in blocked_soids:          # fully blocked — skip
            continue
        db_status = str(row_data["return_status"] or "").strip()
        if db_status not in _open_statuses:
            continue
        wo_id = row_data.get("work_order_id")
        # Skip rows whose SOID or work_order_id is still present in the Excel file
        if soid in excel_soids:
            continue
        if wo_id is not None and _safe_int(wo_id) in excel_wo_ids:
            continue
        status_updates.append(("UNKNOWN", soid))
        db_rows[soid]["return_status"] = "UNKNOWN"  # keep in-memory state current

    conn.executemany("UPDATE wo_product_detail SET dc_number = ? WHERE soid = ?", dc_updates)
    conn.executemany("UPDATE wo_product_detail SET return_status = ? WHERE soid = ?", status_updates)

    # ── Pass 5a: promote any row that already has a real dc_number ────────────
    # Catches rows whose dc_number was set in a previous import but whose
    # return_status was not yet DC GENERATED — including rows sitting at UNKNOWN.
    # NOTE: dc_lenovo alone never triggers this — only dc_number counts.
    conn.execute(
        """
        UPDATE wo_product_detail
           SET return_status = 'DC GENERATED'
         WHERE dc_number IS NOT NULL
           AND dc_number != ''
           AND (return_status IS NULL OR return_status != 'DC GENERATED')
        """
    )

    # ── Pass 5b: clear orphaned DC GENERATED on rows with no real dc_number ────
    # If dc_number is NULL or empty but return_status is 'DC GENERATED', the
    # status is inconsistent (dc_lenovo alone is not enough) — clear back to NULL.
    conn.execute(
        """
        UPDATE wo_product_detail
           SET return_status = NULL
         WHERE return_status = 'DC GENERATED'
           AND (dc_number IS NULL OR dc_number = '')
        """
    )

    conn.commit()
    return len(dc_updates), len(status_updates)


# ── AWB RESOLV upsert ────────────────────────────────────────────────────────

def upsert_awb_resolv_from_awb_excel(
    awb_dir: str, conn: sqlite3.Connection
) -> tuple[int, int]:
    """Read the latest Generated_DCs_*.xlsx from awb_dir and upsert awb_resolv
    and dc_generate_date on wo_product_detail rows by matching Excel DC NO → db dc_number.

    Excel columns used:
        A = DC NO   → match against wo_product_detail.dc_number
        B = AWB NO  → write to wo_product_detail.awb_resolv
        D = DC Generation Date → write to wo_product_detail.dc_generate_date

    Rules:
    - Only DB rows where dc_number IS NOT NULL and dc_number != '' are candidates.
    - awb_resolv and dc_generate_date are only written when the incoming value differs
      from what is already stored (change-detection — avoids spurious writes on every run).
    - If no Generated_DCs_*.xlsx file exists in awb_dir, returns (0, 0) silently.

    Returns:
        (matched_dc, new_updates) where:
            matched_dc  — number of distinct DC numbers that had at least one row
                           actually written (0 when nothing changed)
            new_updates — number of individual DB rows whose columns changed
    """
    import os as _os
    import glob as _glob
    import math as _math

    # Find the latest Generated_DCs_*.xlsx (lexicographic sort on timestamp suffix)
    pattern = _os.path.join(awb_dir, "Generated_DCs_*.xlsx")
    files = sorted(_glob.glob(pattern))
    if not files:
        return 0, 0
    latest = files[-1]

    df = pd.read_excel(latest, sheet_name="Generated DCs")

    def _has_val(v) -> bool:
        if v is None:
            return False
        if isinstance(v, float) and _math.isnan(v):
            return False
        return str(v).strip() not in ("", "nan", "nat", "none", "null", "NaT")

    def _normalise(v) -> str:
        """Normalise whole-number floats: 15835.0 → '15835'."""
        try:
            f = float(v)
            if f == int(f):
                return str(int(f))
        except (ValueError, TypeError):
            pass
        return str(v).strip()

    def _format_date(v) -> str:
        if not _has_val(v):
            return ""
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        s = str(v).strip()
        if " " in s:
            s = s.split(" ")[0]
        return s

    # Build dc_number → (awb_no, dc_generate_date) map from Excel (last row wins on duplicates)
    dc_to_data: dict[str, tuple[str, str]] = {}
    
    dc_col = next((c for c in df.columns if str(c).upper().strip() == "DC NO"), None)
    awb_col = next((c for c in df.columns if str(c).upper().strip() == "AWB NO"), None)
    date_col = next(
        (c for c in df.columns if str(c).upper().strip() in ("DC GENERATION DATE", "DC GENERATE DATE")),
        None
    )

    for _, row in df.iterrows():
        if dc_col is None or awb_col is None:
            continue
        dc_val  = row.get(dc_col)
        awb_val = row.get(awb_col)
        date_val = row.get(date_col) if date_col is not None else None
        
        if not _has_val(dc_val) or not _has_val(awb_val):
            continue
            
        formatted_date = _format_date(date_val)
        dc_to_data[_normalise(dc_val)] = (_normalise(awb_val), formatted_date)

    if not dc_to_data:
        return 0, 0

    # Fetch all (soid, dc_number, awb_resolv, dc_generate_date) for rows that have a real dc_number.
    db_rows = conn.execute(
        "SELECT soid, dc_number, awb_resolv, dc_generate_date FROM wo_product_detail "
        "WHERE dc_number IS NOT NULL AND dc_number != ''"
    ).fetchall()

    updates: list[tuple] = []
    matched_dcs: set[str] = set()   # distinct DC numbers that produced a real write
    for soid, dc_number, current_awb, current_date in db_rows:
        dc_key = str(dc_number).strip()
        incoming_data = dc_to_data.get(dc_key)
        if incoming_data is None:
            continue
        incoming_awb, incoming_date = incoming_data
        
        # Only write when the stored value differs from the incoming value.
        stored_awb = str(current_awb).strip() if current_awb is not None else ""
        stored_date = str(current_date).strip() if current_date is not None else ""
        
        if stored_awb == incoming_awb and stored_date == incoming_date:
            continue
            
        updates.append((incoming_awb, incoming_date, soid))
        matched_dcs.add(dc_key)

    if updates:
        conn.executemany(
            "UPDATE wo_product_detail SET awb_resolv = ?, dc_generate_date = ? WHERE soid = ?",
            updates,
        )
        conn.commit()

    # matched_dcs: distinct DC numbers that had at least one row actually changed.
    return len(matched_dcs), len(updates)


# ── UNRETURN upsert ──────────────────────────────────────────────────────────

def upsert_from_unreturn(df: pd.DataFrame, conn: sqlite3.Connection) -> int:
    """Update wo_product_detail using the ID-IBM ID POU Unreturn Excel file.

    Match key: Excel "SOID" → wo_product_detail.soid

    ── Existing columns (unchanged behaviour) ───────────────────────────────
    dc_lenovo
        Written from "DC/Collection Form" only when it has a real value
        (not NULL, not empty, not "0", not "—").

    return_status
        Not modified by this function.

    ── New columns (date-gated) ─────────────────────────────────────────────
    awb_return            ← "AWB Number"   (real AWB string, "Hardclose", or NULL)
    lenovo_return_status  ← "Return Status"
    awb_notes             ← "Note"
    unreturn_submitted_date ← "DC/Collection Form-Submitted Date" stored as
                              YYYY-MM-DD (converted from DD-MM-YYYY in Excel).

    Date-gate rule (per SOID):
        A row qualifies to write the new columns when:
          (a) "DC/Collection Form-Submitted Date" is a parseable DD-MM-YYYY date, AND
          (b) the stored unreturn_submitted_date is NULL/empty
              OR the incoming ISO date is strictly later than the stored one.
        When the gate passes, ALL four new columns are written together so the
        stored date always matches the data in the other three columns.
        "Hardclose" in AWB Number is treated as a valid value and stored as-is.

    Returns the total number of individual column-writes made.
    """
    import math as _math
    from datetime import datetime as _dt

    def _has_val(v) -> bool:
        if v is None:
            return False
        if isinstance(v, float) and _math.isnan(v):
            return False
        s = str(v).strip()
        return s not in ("", "nan", "nat", "none", "null", "NaT", "0")

    def _to_str_or_none(v):
        """Return stripped string, or None when the cell is empty/NaN."""
        if v is None:
            return None
        if isinstance(v, float) and _math.isnan(v):
            return None
        s = str(v).strip()
        return None if s in ("", "nan", "nat", "none", "null", "NaT") else s

    def _to_str_or_empty(v):
        """Like _to_str_or_none but returns '' instead of None (for AWB Number
        which may legitimately be an empty string meaning 'not assigned yet')."""
        result = _to_str_or_none(v)
        return result  # keep as None so DB stores NULL for unassigned AWB

    def _parse_submitted_date(v) -> str | None:
        """Convert 'DD-MM-YYYY' string → 'YYYY-MM-DD' ISO string.
        Returns None if unparseable so the row is skipped for the new columns.
        """
        raw = _to_str_or_none(v)
        if not raw:
            return None
        try:
            return _dt.strptime(raw, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    # ── Snapshot current DB state ─────────────────────────────────────────
    db_rows: dict[int, dict] = {
        r[0]: {
            "dc_lenovo":             r[1],
            "return_status":         r[2],
            "awb_return":            r[3],
            "lenovo_return_status":  r[4],
            "awb_notes":             r[5],
            "modify_date_dc_lenovo": r[6],
            "is_exist_excel":        r[7],
        }
        for r in conn.execute(
            """SELECT soid, dc_lenovo, return_status,
                      awb_return, lenovo_return_status, awb_notes,
                      modify_date_dc_lenovo, is_exist_excel
               FROM wo_product_detail"""
        ).fetchall()
    }

    # ── Compute file-level version stamp from the Excel file ──────────────
    # max(DC/Collection Form-Submitted Date) across all parseable rows in the
    # incoming file.  This single date is compared against the stored
    # modify_date_dc_lenovo to decide whether the new-column block is written.
    max_submitted_iso: str | None = None
    for _, _row in df.iterrows():
        _d = _parse_submitted_date(_row.get("DC/Collection Form-Submitted Date"))
        if _d and (max_submitted_iso is None or _d > max_submitted_iso):
            max_submitted_iso = _d

    # The single stored stamp is the MAX already in the DB (all rows share the
    # same value after the first upload; MAX handles any legacy per-SOID values).
    _stored_stamp_row = conn.execute(
        "SELECT MAX(modify_date_dc_lenovo) FROM wo_product_detail"
        " WHERE modify_date_dc_lenovo IS NOT NULL"
    ).fetchone()
    stored_stamp: str | None = _stored_stamp_row[0] if _stored_stamp_row else None

    # File-level gate decision (applied uniformly to all rows):
    #   • max_submitted_iso present AND > stored_stamp (or stored_stamp is NULL) → write
    #   • max_submitted_iso present AND ≤ stored_stamp                           → skip all new cols
    #   • max_submitted_iso absent (no parseable dates in file)                  → first-time fill only
    #     (per-SOID cols-null check still applies so existing data is never overwritten blindly)
    if max_submitted_iso:
        file_gate_pass = (stored_stamp is None) or (max_submitted_iso > stored_stamp)
    else:
        file_gate_pass = None   # None = "no date info — fall back to per-SOID null check"

    dc_updates:     list[tuple] = []   # (dc_lenovo_value, soid)
    status_updates: list[tuple] = []   # (return_status_value, soid)
    # New columns — all written atomically per SOID when date gate passes
    new_col_updates: list[tuple] = []  # (awb_return, lenovo_return_status, awb_notes, soid)
    # Tracks every SOID that receives any write this upsert (used to stamp modify_date_dc_lenovo)
    touched_soids:  set[int]     = set()

    # ── Pass 1: stage dc_lenovo values (real values only, changed only) ──────
    # Runs unconditionally — not gated on the file date stamp.
    # dc_lenovo is always written when the Excel DC/Collection Form has a real
    # value that differs from what is already stored, regardless of whether the
    # file is newer or older than the last upload.
    for _, row in df.iterrows():
        soid = _safe_int(row.get("SOID"))
        if soid is None or soid not in db_rows:
            continue
        dc_val = _to_str_or_none(row.get("DC/Collection Form"))
        if not _has_val(dc_val):
            continue  # skip rows where DC/Collection Form is empty, 0, or —
        current_dc = _to_str_or_none(db_rows[soid]["dc_lenovo"])
        if dc_val == current_dc:
            continue  # value unchanged — no write needed
        dc_updates.append((dc_val, soid))
        touched_soids.add(soid)

    # ── Pass 5: is_exist_excel ────────────────────────────────────────────────
    # Runs unconditionally — not gated on the file date stamp.
    # Build the set of SOIDs that appear in the uploaded Excel file.
    excel_soids: set[int] = set()
    for _, _xe_row in df.iterrows():
        _xe_soid = _safe_int(_xe_row.get("SOID"))
        if _xe_soid is not None:
            excel_soids.add(_xe_soid)

    is_exist_updates: list[tuple] = []  # (value, soid)
    for soid, v in db_rows.items():
        current_val = _to_str_or_none(v["is_exist_excel"])
        if soid in excel_soids:
            # SOID found in this excel → mark/keep 'yes'
            if current_val != "yes":
                is_exist_updates.append(("yes", soid))
        else:
            # SOID absent from this excel → demote to 'no' only if previously non-NULL
            if current_val is not None:
                is_exist_updates.append(("no", soid))
            # else: was NULL → stays NULL (never appeared in any excel)

    # ── Gate guard: when the file is not newer, skip file-driven passes ──────
    # file_gate_pass = False means this file's max date is ≤ the stored stamp.
    # Pass 5 (is_exist_excel) above already ran.
    #
    # Exception — null-fill: all three new columns (awb_return, lenovo_return_status,
    # awb_notes) are written atomically even when the gate is blocked, provided ALL
    # three DB values are currently NULL for that SOID (first-time fill).
    # This ensures rows that were never touched by a previous upload get their
    # values regardless of whether the file stamp has advanced.
    #
    # lenovo_return_status lock rule (applies everywhere):
    #   • NULL → can be written (first-time fill)
    #   • "Unreturned" → can be overwritten with any new value from Excel
    #   • Any other value → locked; never overwritten
    if file_gate_pass is False:
        # dc_lenovo was already staged and will be written after this block.
        null_fill_updates: list[tuple] = []  # (awb_return, lenovo_return_status, awb_notes, soid)
        for _, _gf_row in df.iterrows():
            _gf_soid = _safe_int(_gf_row.get("SOID"))
            if _gf_soid is None or _gf_soid not in db_rows:
                continue
            _gf_db = db_rows[_gf_soid]
            _gf_db_lrs = _to_str_or_none(_gf_db["lenovo_return_status"])
            # Only null-fill when ALL three target columns allow writing:
            #   awb_return and awb_notes must be NULL;
            #   lenovo_return_status must be NULL or "Unreturned" (unlocked).
            _lrs_unlocked = (
                _gf_db_lrs is None
                or _gf_db_lrs.strip().lower() == "unreturned"
            )
            if not (
                _to_str_or_none(_gf_db["awb_return"]) is None
                and _lrs_unlocked
                and _to_str_or_none(_gf_db["awb_notes"]) is None
            ):
                continue
            _gf_awb   = _to_str_or_none(_gf_row.get("AWB Number"))
            _gf_rs    = _to_str_or_none(_gf_row.get("Return Status"))
            _gf_notes = _to_str_or_none(_gf_row.get("Note"))
            # Nothing to write at all
            if _gf_awb is None and _gf_rs is None and _gf_notes is None:
                continue
            null_fill_updates.append((_gf_awb, _gf_rs, _gf_notes, _gf_soid))
            touched_soids.add(_gf_soid)

        conn.executemany(
            "UPDATE wo_product_detail SET is_exist_excel = ? WHERE soid = ?",
            is_exist_updates,
        )
        conn.executemany(
            "UPDATE wo_product_detail SET dc_lenovo = ? WHERE soid = ?",
            dc_updates,
        )
        conn.executemany(
            "UPDATE wo_product_detail SET return_status = ? WHERE soid = ?",
            status_updates,
        )
        if null_fill_updates:
            conn.executemany(
                """UPDATE wo_product_detail
                      SET awb_return = ?,
                          lenovo_return_status = ?,
                          awb_notes = ?
                    WHERE soid = ?""",
                null_fill_updates,
            )
        if max_submitted_iso and touched_soids:
            conn.executemany(
                "UPDATE wo_product_detail SET modify_date_dc_lenovo = ? WHERE soid = ?",
                [(max_submitted_iso, s) for s in touched_soids],
            )
        conn.commit()
        return len(dc_updates) + len(status_updates) + len(is_exist_updates) + len(null_fill_updates)

    # ── Pass 3: awb_return / lenovo_return_status / awb_notes (date-gated) ─
    #
    # The date gate is FILE-LEVEL (not per-SOID):
    #   file_gate_pass = True  → this file is newer than the last upload; write all rows
    #   file_gate_pass = False → handled above (null-fill only); skip here
    #   file_gate_pass = None  → no dates in file; first-time fill only (cols-null check)
    #
    # Per-row rules (applied when the gate passes):
    #   • Per-column protection: if the DB already has a value and the incoming cell is
    #     empty, the existing DB value is kept.
    #   • Skip a row entirely when all three effective values match the DB.

    # Short-circuit: if the file gate is explicitly False, null-fill already ran above.
    if file_gate_pass is False:
        pass  # new_col_updates stays empty
    else:
        for _, row in df.iterrows():
            soid = _safe_int(row.get("SOID"))
            if soid is None or soid not in db_rows:
                continue

            db_awb        = _to_str_or_none(db_rows[soid]["awb_return"])
            db_lrs        = _to_str_or_none(db_rows[soid]["lenovo_return_status"])
            db_notes      = _to_str_or_none(db_rows[soid]["awb_notes"])
            cols_are_null = (db_awb is None and db_lrs is None and db_notes is None)

            # When file_gate_pass is None (no date info), only write first-time rows
            if file_gate_pass is None and not cols_are_null:
                continue

            awb_val   = _to_str_or_none(row.get("AWB Number"))   # "Hardclose" kept as-is
            rs_val    = _to_str_or_none(row.get("Return Status"))
            notes_val = _to_str_or_none(row.get("Note"))

            # lenovo_return_status lock rule:
            #   NULL or "Unreturned" → unlocked, can be overwritten
            #   Any other value      → locked, keep existing DB value regardless of Excel
            _lrs_locked = (
                db_lrs is not None
                and db_lrs.strip().lower() != "unreturned"
            )
            if _lrs_locked:
                rs_val = db_lrs  # preserve locked value

            # Per-column protection: don't overwrite other existing DB values with empty
            if awb_val is None and db_awb is not None:
                awb_val = db_awb
            if rs_val is None and db_lrs is not None:
                rs_val = db_lrs
            if notes_val is None and db_notes is not None:
                notes_val = db_notes

            # Skip the row entirely when all three effective values match what is stored
            if awb_val == db_awb and rs_val == db_lrs and notes_val == db_notes:
                continue

            new_col_updates.append((awb_val, rs_val, notes_val, soid))
            touched_soids.add(soid)

    unreturned_clear: list[tuple] = []  # no longer clearing Unreturned values

    # ── Write to DB ───────────────────────────────────────────────────────
    # Note: is_exist_excel and the gate-blocked status_updates are already
    # written and committed in the early-return path above when
    # file_gate_pass is False; they are written here for the normal path.
    conn.executemany(
        "UPDATE wo_product_detail SET is_exist_excel = ? WHERE soid = ?",
        is_exist_updates,
    )
    conn.executemany(
        "UPDATE wo_product_detail SET dc_lenovo = ? WHERE soid = ?",
        dc_updates,
    )
    conn.executemany(
        "UPDATE wo_product_detail SET return_status = ? WHERE soid = ?",
        status_updates,
    )
    conn.executemany(
        """UPDATE wo_product_detail
              SET awb_return = ?,
                  lenovo_return_status = ?,
                  awb_notes = ?
            WHERE soid = ?""",
        new_col_updates,
    )
    if unreturned_clear:
        conn.executemany(
            "UPDATE wo_product_detail SET lenovo_return_status = NULL WHERE soid = ?",
            unreturned_clear,
        )

    # ── Consistency sweep: clear orphaned DC GENERATED with no real dc_number ──
    # Mirrors Pass 5b from upsert_dc_from_gtaap so that a POU Unreturn upload
    # also repairs any row where dc_number is NULL but return_status is
    # 'DC GENERATED' (stale state left from a previous GTAAP import cycle).
    # dc_lenovo alone must never be the basis for DC GENERATED.
    conn.execute(
        """
        UPDATE wo_product_detail
           SET return_status = NULL
         WHERE return_status = 'DC GENERATED'
           AND (dc_number IS NULL OR dc_number = '')
        """
    )

    # ── Stamp modify_date_dc_lenovo for every SOID touched this upsert ────────
    # Written as a single sweep over all touched SOIDs, so every row that
    # received any update (dc_lenovo, awb_return, lenovo_return_status, or
    # awb_notes) carries the same file-level stamp: the MAX of
    # DC/Collection Form-Submitted Date across the uploaded file.
    if max_submitted_iso and touched_soids:
        conn.executemany(
            "UPDATE wo_product_detail SET modify_date_dc_lenovo = ? WHERE soid = ?",
            [(max_submitted_iso, s) for s in touched_soids],
        )
    conn.commit()
    return len(dc_updates) + len(status_updates) + len(new_col_updates) + len(unreturned_clear) + len(is_exist_updates)


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
        n_new_wo, n_updated, n_new_users = upsert_wo_summary_and_details(df, conn)
    elif key == "SOID":
        n_rows = upsert_wo_product_from_msd(df, conn)
    elif key == "SHIPMENT":
        n_rows = upsert_wo_product_from_shipment(df, conn)
    elif key == "PARTONHOLD":
        n_rows = upsert_eta_parthold_from_backlog(df, conn)
        return n_rows  # no orphan purge needed for this category
    elif key == "GTAAP":
        n_new_dc, n_new_status = upsert_dc_from_gtaap(df, conn)
        return n_new_dc, n_new_status  # no orphan purge needed for this category
    elif key == "UNRETURN":
        n_rows = upsert_from_unreturn(df, conn)
        return n_rows  # no orphan purge needed for this category
    else:
        return 0  # unreachable, but satisfies type checkers

    # After every upsert, purge wo_product_detail rows that reference a
    # work_order_id which is no longer present in wo_summary.
    _purge_orphan_product_rows(conn)

    # For WOID, surface all three counts so callers can report stats.
    if key == "WOID":
        return n_new_wo, n_updated, n_new_users  # type: ignore[return-value]
    return n_rows
