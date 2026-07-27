"""
queries.py — Read helpers for the three core SQLite tables.

All functions require a Flask application context (they call get_db()).
Results are returned as plain dicts (not sqlite3.Row objects) so they
are JSON-serialisable directly.
"""

from app.services.database.db import get_db

# ── Shared status-group SQL fragments ────────────────────────────────────────
# "Closed" group: Closed, Completed, RMA In Progress,
#                 Unit Returned to Customer /Awaiting for Parts RMA,
#                 Repair Completed, Ready for Pickup
_CLOSED_WHERE = (
    "LOWER(work_order_status) IN ("
    "'closed','completed','rma in progress',"
    "'unit returned to customer /awaiting for parts rma',"
    "'repair completed','ready for pickup'"
    ")"
)

# "Open" group: everything that is NOT closed AND NOT cancelled
_OPEN_WHERE = (
    "LOWER(work_order_status) NOT IN ("
    "'closed','completed','cancelled','canceled',"
    "'rma in progress',"
    "'unit returned to customer /awaiting for parts rma',"
    "'repair completed','ready for pickup'"
    ")"
)

# "Active" group: Open but excluding Part On Hold and Parts in Transit
_ACTIVE_WHERE = (
    _OPEN_WHERE
    + " AND LOWER(work_order_status) NOT LIKE '%part%hold%'"
    + " AND LOWER(work_order_status) NOT LIKE '%transit%'"
)


def _row_to_dict(row) -> dict:
    return dict(row) if row else {}


def get_all_wo_summary() -> list[dict]:
    """
    Return all rows from wo_summary only (no join), ordered by created_on DESC.
    Used by ASP routes which need the full list for client-side JS filtering.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT work_order_id, serial_number, created_on,
               committed_delivery_date, actual_committed_onsite_date,
               case_desc, work_order_type, contact_name,
               customer, work_order_status
        FROM wo_summary
        ORDER BY created_on DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_wo_summary_page(
    search: str = "",
    status_filter: str = "",
    type_filter: str = "",
    case_status_filter: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """
    Server-side paginated query over wo_summary only.
    Returns { rows: [...], total: int, page: int, pages: int }.

    All filtering is pushed to SQLite so only one page of rows is
    ever transferred to Python / the browser.
    """
    conn   = get_db()
    params: list = []
    wheres: list[str] = []

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(work_order_id AS TEXT) LIKE ?
            OR LOWER(serial_number)     LIKE ?
            OR LOWER(contact_name)      LIKE ?
            OR LOWER(customer)          LIKE ?
            OR LOWER(case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if status_filter:
        sl = status_filter.lower()
        if sl == "active":
            wheres.append(_ACTIVE_WHERE)
        elif sl == "open":
            wheres.append(_OPEN_WHERE)
        elif sl == "closed":
            wheres.append(_CLOSED_WHERE)
        elif sl == "part_hold":
            wheres.append("LOWER(work_order_status) LIKE '%part%hold%'")
        elif sl == "transit":
            wheres.append("LOWER(work_order_status) LIKE '%transit%'")
        else:
            wheres.append("LOWER(work_order_status) LIKE ?")
            params.append(f"%{sl}%")

    if type_filter:
        wheres.append("LOWER(work_order_type) LIKE ?")
        params.append(f"%{type_filter.lower()}%")

    if case_status_filter:
        wheres.append("LOWER(case_status) = ?")
        params.append(case_status_filter.lower())

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM wo_summary {where_sql}", params
    ).fetchone()[0]

    pages  = max(1, -(-total // page_size))   # ceiling division
    offset = (max(1, page) - 1) * page_size

    rows = conn.execute(f"""
        SELECT work_order_id, serial_number, created_on,
               committed_delivery_date, actual_committed_onsite_date,
               case_desc, work_order_type, contact_name,
               customer, work_order_status, case_status
        FROM wo_summary
        {where_sql}
        ORDER BY created_on DESC
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    return {
        "rows":  [dict(r) for r in rows],
        "total": total,
        "page":  page,
        "pages": pages,
    }


def get_wo_detail(work_order_id: int) -> dict:
    """Return a single WO row (summary + details joined) or empty dict."""
    conn = get_db()
    row = conn.execute("""
        SELECT s.*, d.*
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        WHERE s.work_order_id = ?
    """, (work_order_id,)).fetchone()
    return _row_to_dict(row)


def get_parts_for_wo(work_order_id: int) -> list[dict]:
    """Return all part-order lines for a given WO, ordered by line_order."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM wo_product_detail
        WHERE work_order_id = ?
        ORDER BY line_order
    """, (work_order_id,)).fetchall()
    return [dict(r) for r in rows]


def get_wo_by_case_number(case_number: int | str) -> list[dict]:
    """Return all WOs that share the same case_number (ticket), ordered by created_on ASC
    (oldest first). Each row also includes a JSON-encoded 'parts' array of non-cancelled
    part lines (product, description, wo_product_status) for that WO."""
    import json
    conn = get_db()
    # Fetch WO rows
    wo_rows = conn.execute("""
        SELECT s.work_order_id, s.serial_number, s.created_on,
               s.work_order_status, s.case_status, s.case_desc,
               s.work_order_type, s.contact_name, s.customer,
               d.case_number, d.product_id_mtm, d.completion_date, d.closing_date
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        WHERE d.case_number = ?
        ORDER BY s.created_on ASC
    """, (case_number,)).fetchall()

    if not wo_rows:
        return []

    wo_ids = [r["work_order_id"] for r in wo_rows]
    placeholders = ",".join("?" * len(wo_ids))

    # Fetch non-cancelled part lines for all WOs in one query
    part_rows = conn.execute(f"""
        SELECT work_order_id, product, description, wo_product_status
        FROM wo_product_detail
        WHERE work_order_id IN ({placeholders})
          AND LOWER(COALESCE(wo_product_status, '')) NOT LIKE '%cancel%'
        ORDER BY work_order_id, line_order
    """, wo_ids).fetchall()

    # Group parts by work_order_id
    parts_by_wo: dict = {}
    for p in part_rows:
        wid = p["work_order_id"]
        parts_by_wo.setdefault(wid, []).append({
            "product":           p["product"],
            "description":       p["description"],
            "wo_product_status": p["wo_product_status"],
        })

    result = []
    for r in wo_rows:
        row = dict(r)
        row["parts"] = parts_by_wo.get(row["work_order_id"], [])
        result.append(row)
    return result


def get_wo_by_serial(serial_number: str) -> list[dict]:
    """Return all WOs that share the same serial_number (including the current WO),
    ordered by created_on ASC (oldest first)."""
    conn = get_db()
    rows = conn.execute("""
        SELECT s.work_order_id, s.serial_number, s.created_on,
               s.work_order_status, s.case_status, s.case_desc,
               s.work_order_type, s.contact_name, s.customer,
               d.case_number, d.product_id_mtm, d.completion_date
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        WHERE LOWER(s.serial_number) = LOWER(?)
        ORDER BY s.created_on ASC
    """, (serial_number,)).fetchall()
    return [dict(r) for r in rows]


def get_wo_summary_stats() -> dict:
    """
    Return aggregate counts used by stat cards:
        total, closed, open, part_hold, part_transit
    """
    conn = get_db()

    def _count(where: str = "") -> int:
        sql = "SELECT COUNT(*) FROM wo_summary"
        if where:
            sql += " WHERE " + where
        return conn.execute(sql).fetchone()[0]

    total         = _count()
    closed        = _count(_CLOSED_WHERE)
    open_wo       = _count(_OPEN_WHERE)
    part_hold     = _count("LOWER(work_order_status) LIKE '%part%hold%'")
    part_transit  = _count("LOWER(work_order_status) LIKE '%transit%'")

    return {
        "total":         total,
        "closed":        closed,
        "open":          open_wo,
        "part_hold":     part_hold,
        "part_transit":  part_transit,
    }


# ── ASP tab-specific page queries ─────────────────────────────────────────────
# Each tab has a fixed WHERE clause pre-applied, plus optional search/page args.
# Columns returned are the summary-only subset (10 cols) — no join needed here.

_SUMMARY_COLS = """
    work_order_id, serial_number, created_on,
    committed_delivery_date, actual_committed_onsite_date,
    case_desc, work_order_type, contact_name,
    customer, work_order_status, case_status
"""

def _paged_query(
    tab_where: str,
    search: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """
    Internal helper: execute a paginated SELECT over wo_summary with an
    optional pre-applied tab_where clause and an optional free-text search.
    """
    conn   = get_db()
    params: list = []
    wheres: list[str] = []

    if tab_where:
        wheres.append(tab_where)

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(work_order_id AS TEXT) LIKE ?
            OR LOWER(serial_number)     LIKE ?
            OR LOWER(contact_name)      LIKE ?
            OR LOWER(customer)          LIKE ?
            OR LOWER(case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    total  = conn.execute(f"SELECT COUNT(*) FROM wo_summary {where_sql}", params).fetchone()[0]
    pages  = max(1, -(-total // page_size))
    offset = (max(1, page) - 1) * page_size

    rows = conn.execute(f"""
        SELECT {_SUMMARY_COLS}
        FROM wo_summary
        {where_sql}
        ORDER BY created_on DESC
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "pages": pages}


# All WOs — respects extra status, type, and case_status filters passed from the browser
def get_asp_all_wo_page(
    search: str = "", status_filter: str = "", type_filter: str = "",
    case_status_filter: str = "",
    page: int = 1, page_size: int = 25,
) -> dict:
    conn   = get_db()
    params: list = []
    wheres: list[str] = []

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(work_order_id AS TEXT) LIKE ?
            OR LOWER(serial_number)     LIKE ?
            OR LOWER(contact_name)      LIKE ?
            OR LOWER(customer)          LIKE ?
            OR LOWER(case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if status_filter:
        sl = status_filter.lower()
        if sl == "active":
            wheres.append(_ACTIVE_WHERE)
        elif sl == "open":
            wheres.append(_OPEN_WHERE)
        elif sl == "closed":
            wheres.append(_CLOSED_WHERE)
        elif sl == "part_hold":
            wheres.append("LOWER(work_order_status) LIKE '%part%hold%'")
        elif sl == "transit":
            wheres.append("LOWER(work_order_status) LIKE '%transit%'")
        else:
            wheres.append("LOWER(work_order_status) LIKE ?")
            params.append(f"%{sl}%")

    if type_filter:
        wheres.append("LOWER(work_order_type) LIKE ?")
        params.append(f"%{type_filter.lower()}%")

    if case_status_filter:
        wheres.append("LOWER(case_status) = ?")
        params.append(case_status_filter.lower())

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    total  = conn.execute(f"SELECT COUNT(*) FROM wo_summary {where_sql}", params).fetchone()[0]
    pages  = max(1, -(-total // page_size))
    offset = (max(1, page) - 1) * page_size
    rows   = conn.execute(f"""
        SELECT {_SUMMARY_COLS} FROM wo_summary {where_sql}
        ORDER BY created_on DESC LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "pages": pages}


# Part Received — WOs waiting for parts or on part hold or parts in transit
def get_asp_part_received_page(search: str = "", page: int = 1, page_size: int = 25) -> dict:
    tab_where = (
        "LOWER(work_order_status) LIKE '%part%hold%' "
        "OR LOWER(work_order_status) LIKE '%transit%'"
    )
    return _paged_query(tab_where, search, page, page_size)


# CCI Follow-Up — all CCI (Carry-In) WOs with a computed follow-up state
_CCI_FOLLOWUP_COLS = """
    work_order_id, serial_number, created_on,
    committed_delivery_date, actual_committed_onsite_date,
    case_desc, work_order_type, contact_name,
    customer, work_order_status, case_status
"""

def get_asp_cci_followup_page(search: str = "", page: int = 1, page_size: int = 25) -> dict:
    """
    CCI Follow-Up tab — all CCI / Carry-In WOs, any status.
    Adds a computed `followup_state` per row:
        confirm_receipt  — part in transit/hold, ETA not yet passed
        part_sla         — part in transit/hold, ETA already passed
        wo_sla           — part received, WO still open
        input_dc         — WO closed, needs DC / pickup
    """
    import datetime
    conn   = get_db()
    params: list = []
    wheres: list[str] = [
        "(LOWER(work_order_type) LIKE '%carry%' OR LOWER(work_order_type) LIKE '%cci%')"
    ]

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(work_order_id AS TEXT) LIKE ?
            OR LOWER(serial_number)     LIKE ?
            OR LOWER(contact_name)      LIKE ?
            OR LOWER(customer)          LIKE ?
            OR LOWER(case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    where_sql = "WHERE " + " AND ".join(wheres)
    total  = conn.execute(f"SELECT COUNT(*) FROM wo_summary {where_sql}", params).fetchone()[0]
    pages  = max(1, -(-total // page_size))
    offset = (max(1, page) - 1) * page_size

    rows = conn.execute(f"""
        SELECT {_CCI_FOLLOWUP_COLS}
        FROM wo_summary
        {where_sql}
        ORDER BY created_on DESC
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    today = datetime.date.today().isoformat()

    def _followup_state(r: dict) -> str:
        status = (r.get("work_order_status") or "").lower()
        eta    = (r.get("committed_delivery_date") or "")[:10]
        is_closed    = any(k in status for k in ("closed","completed","repair done","rma","returned","pickup"))
        is_cancelled = "cancel" in status
        is_transit   = "transit" in status or "part" in status

        if is_cancelled:
            return "cancelled"
        if is_closed:
            return "input_dc"
        if is_transit:
            return "part_sla" if (eta and eta < today) else "confirm_receipt"
        # open, not transit — part already received, WO not yet closed
        return "wo_sla"

    result_rows = []
    for r in rows:
        row = dict(r)
        row["followup_state"] = _followup_state(row)
        result_rows.append(row)

    return {"rows": result_rows, "total": total, "page": page, "pages": pages}


# Part Return — closed WOs (the full closed group, parts to be returned)
def get_asp_part_return_page(search: str = "", page: int = 1, page_size: int = 25) -> dict:
    tab_where = _CLOSED_WHERE
    return _paged_query(tab_where, search, page, page_size)


# WO Reschedule — open, non-closed, non-cancelled WOs
def get_asp_reschedule_page(search: str = "", page: int = 1, page_size: int = 25) -> dict:
    tab_where = _OPEN_WHERE
    return _paged_query(tab_where, search, page, page_size)
