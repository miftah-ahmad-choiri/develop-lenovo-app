"""
queries.py — Read helpers for the three core SQLite tables.

All functions require a Flask application context (they call get_db()).
Results are returned as plain dicts (not sqlite3.Row objects) so they
are JSON-serialisable directly.
"""

from app.services.database.db import get_db


def isSentinel_py(s) -> bool:
    """Return True if the date string is the sentinel value (year 2099+)."""
    return bool(s) and str(s)[:4] >= "2099"

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
        SELECT s.*, d.*, u.full_name AS tech_name
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        LEFT JOIN asp_users u ON u.tech_id = d.tech_id
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


def get_wo_summary_stats(
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    Return aggregate counts used by stat cards and mobile home badges.

    Mobile-specific keys returned:
        in_prepare_total      — actionable In-Prepare WOs
        cci_followup_total    — actionable CCI Follow-Up WOs
        onsite_followup_total — actionable ONS Follow-Up WOs
        return_part_total     — Return Part WOs awaiting action
        in_return_total       — alias for return_part_total (mobile home card)

    When tech_id_filter is given, only WOs assigned to that technician are counted.
    When vendor_filter is given (asp/asp_master), only that ASP's WOs are counted.
    """
    # Reuse the existing page queries — they already contain all the state
    # computation logic. Fetching page 1 with size 1 is cheap; we only need
    # the `total` field they return.
    in_prepare_total      = get_asp_in_prepare_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter
    )["total"]
    cci_followup_total    = get_asp_cci_followup_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter
    )["total"]
    cci_in_transit_total  = get_asp_cci_followup_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter, followup_state="in_transit"
    )["total"]
    cci_in_repair_total   = get_asp_cci_followup_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter, followup_state="in_repair"
    )["total"]
    onsite_followup_total = get_asp_onsite_followup_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter
    )["total"]
    ons_in_transit_total  = get_asp_onsite_followup_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter, followup_state="wo_reschedule"
    )["total"]
    ons_in_repair_total   = get_asp_onsite_followup_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter, followup_state="wo_sla"
    )["total"]
    return_part_total     = get_asp_part_return_page(
        page_size=1, vendor_filter=vendor_filter, tech_id_filter=tech_id_filter, followup_state="need_to_return"
    )["total"]

    conn = get_db()

    if tech_id_filter:
        tf_safe = tech_id_filter.replace("'", "''")
        def _count(extra_where: str = "") -> int:
            clauses = [f"d.tech_id = '{tf_safe}'"]
            if extra_where:
                clauses.append(extra_where.replace("work_order_status", "s.work_order_status"))
            where = " AND ".join(clauses)
            sql = (
                "SELECT COUNT(*) FROM wo_summary s "
                "LEFT JOIN wo_details d USING (work_order_id) "
                f"WHERE {where}"
            )
            return conn.execute(sql).fetchone()[0]
    elif vendor_filter:
        vf_safe = vendor_filter.replace("'", "''")
        def _count(extra_where: str = "") -> int:
            clauses = [f"d.labor_vendor_related = '{vf_safe}'"]
            if extra_where:
                clauses.append(extra_where.replace("work_order_status", "s.work_order_status"))
            where = " AND ".join(clauses)
            sql = (
                "SELECT COUNT(*) FROM wo_summary s "
                "LEFT JOIN wo_details d USING (work_order_id) "
                f"WHERE {where}"
            )
            return conn.execute(sql).fetchone()[0]
    else:
        def _count(where: str = "") -> int:
            sql = "SELECT COUNT(*) FROM wo_summary"
            if where:
                sql += " WHERE " + where
            return conn.execute(sql).fetchone()[0]

    total        = _count()
    closed       = _count(_CLOSED_WHERE)
    open_wo      = _count(_OPEN_WHERE)
    part_hold    = _count("LOWER(work_order_status) LIKE '%part%hold%'")
    part_transit = _count("LOWER(work_order_status) LIKE '%transit%'")

    return {
        # legacy web portal keys
        "total":               total,
        "closed":              closed,
        "open":                open_wo,
        "part_hold":           part_hold,
        "part_transit":        part_transit,
        # mobile home badge keys
        "in_prepare_total":      in_prepare_total,
        "cci_followup_total":    cci_followup_total,
        "onsite_followup_total": onsite_followup_total,
        "return_part_total":     return_part_total,
        "in_return_total":       return_part_total,
        # mobile CCI subtab badge keys
        "cci_in_transit_total":  cci_in_transit_total,
        "cci_in_repair_total":   cci_in_repair_total,
        # mobile ONS subtab badge keys
        "ons_in_transit_total":  ons_in_transit_total,
        "ons_in_repair_total":   ons_in_repair_total,
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
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    Internal helper: execute a paginated SELECT over wo_summary with an
    optional pre-applied tab_where clause, optional free-text search, and
    an optional vendor/tech_id filter.
    tech_id_filter takes priority: when set, only WOs assigned to that
    technician (wo_details.tech_id) are returned.
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

    if tech_id_filter:
        wheres.append(
            "work_order_id IN ("
            "SELECT work_order_id FROM wo_details "
            "WHERE tech_id = ?)"
        )
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append(
            "work_order_id IN ("
            "SELECT work_order_id FROM wo_details "
            "WHERE labor_vendor_related = ?)"
        )
        params.append(vendor_filter)

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
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
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

    if tech_id_filter:
        wheres.append(
            "work_order_id IN ("
            "SELECT work_order_id FROM wo_details "
            "WHERE tech_id = ?)"
        )
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append(
            "work_order_id IN ("
            "SELECT work_order_id FROM wo_details "
            "WHERE labor_vendor_related = ?)"
        )
        params.append(vendor_filter)

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
def get_asp_part_received_page(
    search: str = "", page: int = 1, page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    tab_where = (
        "LOWER(work_order_status) LIKE '%part%hold%' "
        "OR LOWER(work_order_status) LIKE '%transit%'"
    )
    return _paged_query(tab_where, search, page, page_size, vendor_filter, tech_id_filter)


# CCI Follow-Up — all CCI (Carry-In) WOs with a computed follow-up state
_CCI_FOLLOWUP_COLS = """
    s.work_order_id, s.serial_number, s.created_on,
    s.committed_delivery_date, s.actual_committed_onsite_date,
    s.case_desc, s.work_order_type, s.contact_name,
    s.customer, s.work_order_status, s.case_status,
    d.completion_date, d.closing_date, d.customer_defer_date,
    u.full_name AS tech_name,
    (SELECT awb FROM wo_product_detail
     WHERE wo_product_detail.work_order_id = s.work_order_id
       AND TRIM(COALESCE(awb,'')) != ''
     ORDER BY soid DESC LIMIT 1) AS part_awb,
    (SELECT COUNT(*)
     FROM wo_product_detail p
     WHERE p.work_order_id = s.work_order_id
       AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
       AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
       AND TRIM(COALESCE(p.ship_pou_pod_time,'')) = ''
       AND TRIM(COALESCE(p.delivery_date,''))     = '') AS part_qty,
    (SELECT COALESCE(NULLIF(TRIM(p.ship_pickup_time),''), NULLIF(TRIM(p.shipment_date),''))
     FROM wo_product_detail p
     WHERE p.work_order_id = s.work_order_id
       AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
       AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
     ORDER BY p.soid DESC LIMIT 1) AS ship_pickup_time,
    (SELECT NULLIF(TRIM(p.target),'')
     FROM wo_product_detail p
     WHERE p.work_order_id = s.work_order_id
       AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
       AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
     ORDER BY p.soid DESC LIMIT 1) AS part_eta,
    (SELECT EXISTS(
         SELECT 1
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
           AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
           AND (
             TRIM(COALESCE(p.ship_pickup_time,'')) != ''
             OR TRIM(COALESCE(p.shipment_date,'')) != ''
           )
     )) AS part_shipped,
    (SELECT COALESCE(NULLIF(TRIM(p.ship_pou_pod_time),''), NULLIF(TRIM(p.delivery_date),''))
     FROM wo_product_detail p
     WHERE p.work_order_id = s.work_order_id
       AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
       AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
     ORDER BY p.soid DESC LIMIT 1) AS part_pod_time,
    (SELECT EXISTS(
         SELECT 1
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
           AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
           AND (
             TRIM(COALESCE(p.ship_pou_pod_time,'')) != ''
             OR TRIM(COALESCE(p.delivery_date,'')) != ''
           )
     )) AS part_pod,
    (SELECT (p.dc_number IS NOT NULL AND TRIM(COALESCE(p.dc_number,'')) != '')
     FROM wo_product_detail p
     WHERE p.work_order_id = s.work_order_id
       AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
       AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
     ORDER BY p.soid DESC LIMIT 1) AS part_dc_filled
"""

# SLA thresholds: Carry-In/CCI = 1 day, Onsite = 3.75 days (in hours)
_CCI_SLA_HOURS    = 24        # 1 day
_ONSITE_SLA_HOURS = 90        # 3.75 days


def get_asp_cci_followup_page(
    search: str = "",
    followup_state: str = "",
    page: int = 1,
    page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    CCI Follow-Up tab — all CCI / Carry-In WOs, excluding cancelled.
    Adds a computed `followup_state` per row:
        confirm_receipt  — part in transit/hold, ETA not yet passed
        part_sla         — part in transit/hold, ETA already passed
        wo_sla           — part delivered (actual_committed_onsite_date set),
                           WO completion_date and closing_date both empty,
                           and elapsed time since delivery <= 1 day
        report_problem   — same as wo_sla but elapsed time > 1 day
        input_dc         — WO closed (completion_date or closing_date set)

    When `followup_state` is given, only rows matching that computed state
    are returned (filtering happens in Python after state computation so
    pagination counts are accurate per-state).
    """
    import datetime
    conn   = get_db()
    params: list = []
    wheres: list[str] = [
        "(LOWER(s.work_order_type) LIKE '%carry%' OR LOWER(s.work_order_type) LIKE '%cci%')",
        "LOWER(COALESCE(s.work_order_status,'')) NOT LIKE '%cancel%'",
    ]

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(s.work_order_id AS TEXT) LIKE ?
            OR LOWER(s.serial_number)     LIKE ?
            OR LOWER(s.contact_name)      LIKE ?
            OR LOWER(s.customer)          LIKE ?
            OR LOWER(s.case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if tech_id_filter:
        wheres.append("d.tech_id = ?")
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append("d.labor_vendor_related = ?")
        params.append(vendor_filter)

    where_sql = "WHERE " + " AND ".join(wheres)
    # DB timestamps are in WIB (UTC+7) — use WIB now so elapsed_h is correct
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)

    def _followup_state(r: dict) -> str:
        # Use part_eta (target col from Shipment file), NOT committed_delivery_date
        part_eta_raw = (r.get("part_eta") or "")[:10]
        today        = now.date().isoformat()
        pod_raw        = r.get("part_pod_time") or ""
        comp_date      = r.get("completion_date") or ""
        close_date     = r.get("closing_date") or ""
        is_closed      = bool(comp_date or close_date)
        part_shipped   = bool(r.get("part_shipped"))
        latest_has_pod = bool(pod_raw)
        # part pod = ship_pou_pod_time or delivery_date filled on any active part order
        part_pod       = bool(r.get("part_pod"))
        # AWB filled on any part line for this WO
        part_awb     = bool(r.get("part_awb"))

        # part shipped but NOT yet POD'd
        if part_shipped and not part_pod:
            # part_sla when:
            #   - part_eta is filled (target date exists)
            #   - we are strictly past ETA (date has already passed — not same day)
            # AWB presence is NOT a gate — a part can have its AWB confirmed
            # but still not have arrived (no ship_pou_pod_time / delivery_date).
            # In that case, if ETA has passed it is still a Part SLA breach.
            is_sla_breach = (
                bool(part_eta_raw)
                and part_eta_raw < today      # overdue only when ETA date is in the past
            )
            return "part_sla" if is_sla_breach else "confirm_receipt"
        # part received at ASP (POD filled) and WO still open — only treat as CCI follow-up
        # when there is no newer unshipped part stage that should keep the WO in In-Prepare.
        if part_pod and not is_closed and not (part_shipped and not latest_has_pod):
            elapsed_h = 0
            if pod_raw and not isSentinel_py(pod_raw):
                try:
                    pod_dt    = datetime.datetime.fromisoformat(
                        str(pod_raw)[:16].replace(" ", "T")
                    )
                    elapsed_h = (now - pod_dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    elapsed_h = 0
            return "report_problem" if elapsed_h > _CCI_SLA_HOURS else "wo_sla"
        # no part order / all cancelled, dc already filled, or not yet shipped
        return ""

    all_rows = conn.execute(f"""
        SELECT {_CCI_FOLLOWUP_COLS}
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        LEFT JOIN asp_users u ON u.tech_id = d.tech_id
        {where_sql}
        ORDER BY
            CASE WHEN NULLIF(TRIM(COALESCE(
                     (SELECT target FROM wo_product_detail p2
                      WHERE p2.work_order_id = s.work_order_id
                        AND LOWER(COALESCE(p2.wo_product_status,'')) NOT LIKE '%cancel%'
                        AND TRIM(COALESCE(p2.order_date, p2.acceptance_date,'')) != ''
                      ORDER BY p2.soid DESC LIMIT 1)
                 ,'')),'' ) IS NULL THEN 1 ELSE 0 END ASC,
            NULLIF(TRIM(COALESCE(
                     (SELECT target FROM wo_product_detail p2
                      WHERE p2.work_order_id = s.work_order_id
                        AND LOWER(COALESCE(p2.wo_product_status,'')) NOT LIKE '%cancel%'
                        AND TRIM(COALESCE(p2.order_date, p2.acceptance_date,'')) != ''
                      ORDER BY p2.soid DESC LIMIT 1)
                 ,'')),'' ) ASC,
            NULLIF(TRIM(COALESCE(
                     (SELECT COALESCE(NULLIF(TRIM(p2.ship_pickup_time),''), NULLIF(TRIM(p2.shipment_date),''))
                      FROM wo_product_detail p2
                      WHERE p2.work_order_id = s.work_order_id
                        AND LOWER(COALESCE(p2.wo_product_status,'')) NOT LIKE '%cancel%'
                        AND TRIM(COALESCE(p2.order_date, p2.acceptance_date,'')) != ''
                      ORDER BY p2.soid DESC LIMIT 1)
                 ,'')),'' ) ASC
    """, params).fetchall()

    if followup_state:
        # Named filter groups:
        #   in_transit  — part shipped, not yet received (confirm_receipt + part_sla)
        #   in_repair   — part received, WO still open   (wo_sla + report_problem)
        #   confirm_receipt — legacy alias, same as in_transit
        if followup_state in ("in_transit", "confirm_receipt"):
            filter_states = {"confirm_receipt", "part_sla"}
        elif followup_state == "in_repair":
            filter_states = {"wo_sla", "report_problem"}
        else:
            filter_states = {followup_state}
        filtered = []
        for r in all_rows:
            row = dict(r)
            state = _followup_state(row)
            if state in filter_states:
                row["followup_state"] = state
                filtered.append(row)

        total  = len(filtered)
        pages  = max(1, -(-total // page_size))
        offset = (max(1, page) - 1) * page_size
        result_rows = filtered[offset: offset + page_size]
    else:
        # "All Follow-Up States" view — only rows with an actionable state
        result_rows_all = []
        for r in all_rows:
            row = dict(r)
            state = _followup_state(row)
            if not state:
                continue
            row["followup_state"] = state
            result_rows_all.append(row)

        # Sort by the computed target datetime oldest-first:
        #   confirm_receipt / part_sla  → part_eta (YYYY-MM-DD or datetime string)
        #   wo_sla / report_problem     → part_pod_time + 1 day
        #   rows with no computable target go last
        def _target_sort_key(row: dict) -> str:
            state = row.get("followup_state", "")
            # wo_sla / report_problem: sort key = part_pod_time + 1 day (exact datetime)
            # confirm_receipt / part_sla: sort key = part_eta date padded to T23:59
            #   so that wo_sla/report_problem rows whose threshold falls on the same
            #   calendar day always appear BEFORE confirm_receipt/part_sla rows.
            if state in ("wo_sla", "report_problem"):
                pod = (row.get("part_pod_time") or "").strip()[:16]
                if pod:
                    try:
                        pod_dt = datetime.datetime.fromisoformat(pod.replace(" ", "T"))
                        thresh = pod_dt + datetime.timedelta(days=1)
                        return thresh.isoformat()[:16]
                    except (ValueError, TypeError):
                        pass
            if state in ("confirm_receipt", "part_sla"):
                eta = (row.get("part_eta") or "")[:10].strip()
                return f"{eta}T23:59" if eta else "9999"
            return "9999"

        result_rows_all.sort(key=_target_sort_key)
        result_rows = result_rows_all

        total  = len(result_rows)
        pages  = max(1, -(-total // page_size))
        offset = (max(1, page) - 1) * page_size
        result_rows = result_rows[offset: offset + page_size]

    return {"rows": result_rows, "total": total, "page": page, "pages": pages}


# Part Return — closed WOs that have a return_status set on any part line
def get_asp_part_return_page(
    search: str = "",
    followup_state: str = "",
    page: int = 1,
    page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    Return Part Follow-Up — closed/completed WOs that have at least one non-cancelled
    part line with a known return_status value.  Computed followup_state per row:
        need_to_return — any active part line has return_status =
                         'PENDING WITH PARTNER' or 'PENDING FOR DC GENERATION'
        dc_generated   — no pending lines; relevant lines have return_status = 'DC GENERATED'
    WOs with no return_status on any part line are excluded entirely.
    """
    conn = get_db()
    params: list = []

    cols = """
        s.work_order_id, s.serial_number, s.created_on,
        s.committed_delivery_date, s.actual_committed_onsite_date,
        s.case_desc, s.work_order_type, s.contact_name,
        s.customer, s.work_order_status, s.case_status,
        d.completion_date, d.closing_date,
        u.full_name AS tech_name,
        (SELECT 1
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
               'PENDING WITH PARTNER','PENDING FOR DC GENERATION','DC GENERATED'
           )
         LIMIT 1) AS has_return_status,
        (SELECT 1
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
               'PENDING WITH PARTNER','PENDING FOR DC GENERATION'
           )
         LIMIT 1) AS has_pending_return,
        (SELECT p.return_status
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
               'PENDING WITH PARTNER','PENDING FOR DC GENERATION','DC GENERATED'
           )
         ORDER BY p.soid DESC LIMIT 1) AS return_status,
        (SELECT p.dc_number
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
               'PENDING WITH PARTNER','PENDING FOR DC GENERATION','DC GENERATED'
           )
         ORDER BY p.soid DESC LIMIT 1) AS dc_number,
        (SELECT p.dc_lenovo
         FROM wo_product_detail p
         WHERE p.work_order_id = s.work_order_id
           AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
               'PENDING WITH PARTNER','PENDING FOR DC GENERATION','DC GENERATED'
           )
         ORDER BY p.soid DESC LIMIT 1) AS dc_lenovo
    """

    # Only closed/completed WOs that have at least one part line with a known return_status
    wheres = [
        _CLOSED_WHERE.replace("work_order_status", "s.work_order_status"),
        """EXISTS (
            SELECT 1 FROM wo_product_detail p
            WHERE p.work_order_id = s.work_order_id
              AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
                  'PENDING WITH PARTNER','PENDING FOR DC GENERATION','DC GENERATED'
              )
        )""",
    ]

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(s.work_order_id AS TEXT) LIKE ?
            OR LOWER(s.serial_number)     LIKE ?
            OR LOWER(s.contact_name)      LIKE ?
            OR LOWER(s.customer)          LIKE ?
            OR LOWER(s.case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if tech_id_filter:
        wheres.append("d.tech_id = ?")
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append("d.labor_vendor_related = ?")
        params.append(vendor_filter)

    where_sql = "WHERE " + " AND ".join(wheres)

    def _state(r: dict) -> str:
        if r.get("has_pending_return"):
            return "need_to_return"
        return "dc_generated"

    all_rows = conn.execute(
        f"SELECT {cols} FROM wo_summary s LEFT JOIN wo_details d USING (work_order_id) LEFT JOIN asp_users u ON u.tech_id = d.tech_id {where_sql} ORDER BY s.created_on DESC",
        params,
    ).fetchall()

    if followup_state:
        result_rows = []
        for r in all_rows:
            row = dict(r)
            if _state(row) == followup_state:
                row["followup_state"] = followup_state
                result_rows.append(row)
    else:
        result_rows = []
        for r in all_rows:
            row = dict(r)
            row["followup_state"] = _state(row)
            result_rows.append(row)

    total  = len(result_rows)
    pages  = max(1, -(-total // page_size))
    offset = (max(1, page) - 1) * page_size
    return {"rows": result_rows[offset: offset + page_size], "total": total, "page": page, "pages": pages}


# WOs by AWB — all WOs sharing a given AWB that are still open/transit/part-hold
def get_wos_by_awb(awb: str) -> list[dict]:
    """Return all WOs that have a part line matching the given AWB, including
    their WO number, status, contact, and part details. Used by the Part
    Received confirmation form to allow bulk confirmation across WOs."""
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT
            s.work_order_id, s.contact_name, s.customer,
            s.work_order_status, s.work_order_type,
            s.committed_delivery_date,
            p.soid, p.product, p.description, p.wo_product_status, p.awb
        FROM wo_summary s
        JOIN wo_product_detail p USING (work_order_id)
        WHERE TRIM(p.awb) = ?
          AND LOWER(COALESCE(s.work_order_status,'')) NOT LIKE '%cancel%'
          AND LOWER(COALESCE(s.work_order_type,'')) LIKE '%carry%'
        ORDER BY s.work_order_id ASC
    """, (awb.strip(),)).fetchall()
    return [dict(r) for r in rows]


# WOs with no AWB — open WOs for a given ASP where at least one part line has no AWB set
def get_wo_no_awb_by_asp(customer: str, current_wo_id: int | None = None) -> list[dict]:
    """Return open WOs belonging to the given ASP (customer) that have at
    least one non-cancelled part line where:
      - AWB is NULL or blank (part has no tracking number yet), AND
      - ship_pickup_time or shipment_date is filled (part has actually shipped)
    WO must not be closed / completed / cancelled.

    If current_wo_id is supplied, that WO is always included (prepended) as
    long as it has at least one part line matching the same two conditions —
    so the current WO always appears at the top of the list in the form.
    """
    conn = get_db()
    _closed_statuses = (
        'closed', 'completed', 'cancelled', 'canceled',
        'rma in progress',
        'unit returned to customer /awaiting for parts rma',
        'repair completed', 'ready for pickup',
    )
    _open_where = """
        LOWER(COALESCE(s.work_order_status,'')) NOT IN ({})
        AND LOWER(COALESCE(s.work_order_status,'')) NOT LIKE '%cancel%'
    """.format(','.join('?' * len(_closed_statuses)))

    # Part condition: no AWB yet BUT already shipped (pickup or shipment date filled)
    _part_where = """
        TRIM(COALESCE(p.awb,'')) = ''
        AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
        AND (
            TRIM(COALESCE(p.ship_pickup_time,'')) != ''
            OR TRIM(COALESCE(p.shipment_date,''))  != ''
        )
    """

    no_awb_rows = conn.execute("""
        SELECT
            s.work_order_id, s.contact_name, s.customer,
            s.work_order_status, s.work_order_type,
            s.committed_delivery_date,
            p.soid, p.product, p.description, p.wo_product_status
        FROM wo_summary s
        JOIN wo_product_detail p USING (work_order_id)
        WHERE LOWER(TRIM(s.customer)) = LOWER(TRIM(?))
          AND LOWER(COALESCE(s.work_order_type,'')) LIKE '%carry%'
          AND """ + _part_where + """
          AND """ + _open_where + """
        ORDER BY s.work_order_id ASC
    """, (customer.strip(),) + _closed_statuses).fetchall()

    rows = [dict(r) for r in no_awb_rows]

    # Always include the current WO at the top — but only its part lines that
    # also match: no AWB AND already shipped. This prevents parts with AWBs or
    # unshipped parts from appearing in the "no AWB" list.
    if current_wo_id is not None:
        already_included = any(r['work_order_id'] == current_wo_id for r in rows)
        if not already_included:
            current_rows = conn.execute("""
                SELECT
                    s.work_order_id, s.contact_name, s.customer,
                    s.work_order_status, s.work_order_type,
                    s.committed_delivery_date,
                    p.soid, p.product, p.description, p.wo_product_status
                FROM wo_summary s
                JOIN wo_product_detail p USING (work_order_id)
                WHERE s.work_order_id = ?
                  AND LOWER(COALESCE(s.work_order_type,'')) LIKE '%carry%'
                  AND """ + _part_where + """
                ORDER BY p.line_order ASC
            """, (current_wo_id,)).fetchall()
            if current_rows:
                rows = [dict(r) for r in current_rows] + rows

    return rows


# Return-Part same-ASP — closed WOs for a given ASP with pending return_status lines
def get_return_part_wos_by_asp(customer: str) -> list[dict]:
    """Return closed WOs belonging to the given ASP that have at least one
    non-cancelled part line with return_status PENDING WITH PARTNER or
    PENDING FOR DC GENERATION."""
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT
            s.work_order_id, s.contact_name, s.customer,
            s.work_order_status, s.work_order_type,
            s.committed_delivery_date, s.created_on,
            d.completion_date,
            p.soid, p.product, p.description, p.wo_product_status, p.return_status
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        JOIN wo_product_detail p USING (work_order_id)
        WHERE LOWER(TRIM(s.customer)) = LOWER(TRIM(?))
          AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
              'PENDING WITH PARTNER','PENDING FOR DC GENERATION'
          )
          AND LOWER(s.work_order_status) IN (
              'closed','completed','rma in progress',
              'unit returned to customer /awaiting for parts rma',
              'repair completed','ready for pickup'
          )
        ORDER BY s.work_order_id ASC
    """, (customer.strip(),)).fetchall()
    return [dict(r) for r in rows]


# WO Reschedule — open, non-closed, non-cancelled WOs
def get_asp_reschedule_page(
    search: str = "", page: int = 1, page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    tab_where = _OPEN_WHERE
    return _paged_query(tab_where, search, page, page_size, vendor_filter, tech_id_filter)


# Onsite Follow-Up — all Onsite WOs (excluding cancelled) with a computed follow-up state
def get_asp_onsite_followup_page(
    search: str = "",
    followup_state: str = "",
    page: int = 1,
    page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    Onsite Follow-Up tab — all Onsite WOs, excluding cancelled.
    Computed followup_state per row:
        wo_reschedule  — WO open (no closing/completion date), latest active part
                         line has shipment_date or ship_pickup_time filled,
                         but ship_pou_pod_time and delivery_date are still empty
        wo_sla         — part shipped+POD'd, WO still open, elapsed <= 3.75 days
        report_problem — part shipped+POD'd, WO still open, elapsed > 3.75 days
        input_dc       — WO closed, has a real part order, dc_number not yet filled
    """
    import datetime
    conn   = get_db()
    params: list = []
    wheres: list[str] = [
        "LOWER(s.work_order_type) LIKE '%onsite%'",
        "LOWER(COALESCE(s.work_order_status,'')) NOT LIKE '%cancel%'",
    ]

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(s.work_order_id AS TEXT) LIKE ?
            OR LOWER(s.serial_number)     LIKE ?
            OR LOWER(s.contact_name)      LIKE ?
            OR LOWER(s.customer)          LIKE ?
            OR LOWER(s.case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if tech_id_filter:
        wheres.append("d.tech_id = ?")
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append("d.labor_vendor_related = ?")
        params.append(vendor_filter)

    where_sql = "WHERE " + " AND ".join(wheres)
    # DB timestamps are in WIB (UTC+7) — use WIB now so elapsed_h is correct
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)

    def _followup_state(r: dict) -> str:
        eta          = (r.get("committed_delivery_date") or "")[:10]
        today        = now.date().isoformat()
        delivery_raw = r.get("actual_committed_onsite_date") or ""
        comp_date    = r.get("completion_date") or ""
        close_date   = r.get("closing_date") or ""
        is_closed    = bool(comp_date or close_date)
        # part_shipped is None  → no active part line at all
        # part_shipped is 0     → order exists but not yet shipped
        # part_shipped is 1     → shipment_date or ship_pickup_time is filled
        part_has_order = r.get("part_shipped") is not None
        part_shipped   = bool(r.get("part_shipped"))
        # part_pod = ship_pou_pod_time or delivery_date filled on latest active line
        part_pod       = bool(r.get("part_pod"))
        # dc_number filled on the latest active part line
        part_dc_filled = bool(r.get("part_dc_filled"))

        # WO open + part shipped + not yet POD'd:
        #   ETA already passed → part_sla (shown as alert inside WO Reschedule sub-tab)
        #   ETA not yet passed → wo_reschedule
        if part_shipped and not part_pod and not is_closed:
            return "part_sla" if (eta and eta < today) else "wo_reschedule"
        # part received (POD filled) and WO still open — check WO completion SLA
        if part_pod and not is_closed:
            if delivery_raw and not isSentinel_py(delivery_raw):
                try:
                    delivery_dt = datetime.datetime.fromisoformat(
                        str(delivery_raw)[:16].replace(" ", "T")
                    )
                    elapsed_h = (now - delivery_dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    elapsed_h = 0
            else:
                elapsed_h = 0
            return "report_problem" if elapsed_h > _ONSITE_SLA_HOURS else "wo_sla"
        # Everything else (no part shipped, WO closed, etc.) — no actionable state
        return ""

    def _ons_sort_key(row: dict):
        """Sort by ETA asc (oldest first), no-ETA rows last sorted by ship_pickup_time asc.
        For wo_sla/report_problem rows sort by base+4days Target Fix asc (oldest first)."""
        state = row.get("followup_state", "")
        if state in ("wo_sla", "report_problem"):
            base = (row.get("customer_defer_date") or row.get("part_pod_time")
                    or row.get("actual_committed_onsite_date") or "").strip()[:16]
            if base:
                try:
                    base_dt = datetime.datetime.fromisoformat(base.replace(" ", "T"))
                    thresh  = base_dt + datetime.timedelta(days=4)
                    return (0, thresh.isoformat()[:16], "")
                except (ValueError, TypeError):
                    pass
            return (1, "", "")
        eta = (row.get("part_eta") or "")[:10].strip()
        pickup = (row.get("ship_pickup_time") or row.get("created_on") or "")[:16].strip()
        if eta:
            return (0, eta, pickup)
        return (1, "", pickup)

    def _ons_all_sort_key(row: dict) -> str:
        """All ONS Follow-Up sort: wo_sla/report_problem by base+4days exact datetime,
        wo_reschedule/part_sla by part_eta padded to T23:59 so in-repair rows on the
        same calendar day always sort above in-transit arriving-today rows."""
        state = row.get("followup_state", "")
        if state in ("wo_sla", "report_problem"):
            base = (row.get("customer_defer_date") or row.get("part_pod_time")
                    or row.get("actual_committed_onsite_date") or "").strip()[:16]
            if base:
                try:
                    base_dt = datetime.datetime.fromisoformat(base.replace(" ", "T"))
                    thresh  = base_dt + datetime.timedelta(days=4)
                    return thresh.isoformat()[:16]
                except (ValueError, TypeError):
                    pass
        if state in ("wo_reschedule", "part_sla"):
            eta = (row.get("part_eta") or "")[:10].strip()
            return f"{eta}T23:59" if eta else "9999"
        return "9999"

    if followup_state:
        all_rows = conn.execute(f"""
            SELECT {_CCI_FOLLOWUP_COLS}
            FROM wo_summary s
            LEFT JOIN wo_details d USING (work_order_id)
            LEFT JOIN asp_users u ON u.tech_id = d.tech_id
            {where_sql}
            ORDER BY s.created_on DESC
        """, params).fetchall()

        # wo_reschedule sub-tab also includes part_sla rows (ETA-overdue variant)
        # wo_sla sub-tab also includes report_problem rows (elapsed > 3.75 days)
        # so both appear together under the same In-Repair filter
        if followup_state == "wo_reschedule":
            filter_states = {"wo_reschedule", "part_sla"}
        elif followup_state == "wo_sla":
            filter_states = {"wo_sla", "report_problem"}
        else:
            filter_states = {followup_state}
        filtered = []
        for r in all_rows:
            row = dict(r)
            state = _followup_state(row)
            if state in filter_states:
                row["followup_state"] = state
                filtered.append(row)

        filtered.sort(key=_ons_sort_key)
        total  = len(filtered)
        pages  = max(1, -(-total // page_size))
        offset = (max(1, page) - 1) * page_size
        result_rows = filtered[offset: offset + page_size]
    else:
        # "All Follow-Up States" view
        all_rows = conn.execute(f"""
            SELECT {_CCI_FOLLOWUP_COLS}
            FROM wo_summary s
            LEFT JOIN wo_details d USING (work_order_id)
            LEFT JOIN asp_users u ON u.tech_id = d.tech_id
            {where_sql}
            ORDER BY s.created_on DESC
        """, params).fetchall()

        result_rows = []
        for r in all_rows:
            row = dict(r)
            state = _followup_state(row)
            # Skip rows with no actionable follow-up state
            if not state:
                continue
            row["followup_state"] = state
            result_rows.append(row)

        result_rows.sort(key=_ons_all_sort_key)
        total  = len(result_rows)
        pages  = max(1, -(-total // page_size))
        offset = (max(1, page) - 1) * page_size
        result_rows = result_rows[offset: offset + page_size]

    return {"rows": result_rows, "total": total, "page": page, "pages": pages}


# ── In-Prepare Follow-Up ─────────────────────────────────────────────────────
# A WO is "in prepare" when:
#   PATH A — WO is open AND has at least one non-cancelled part line that has
#             not yet been shipped (ship_pickup_time / shipment_date both empty)
#             and not yet POD'd (ship_pou_pod_time / delivery_date both empty).
#             This covers parts at any pre-shipment stage: Released, ordered,
#             or simply not yet dispatched.
#   PATH B — WO is open AND has NO part lines at all in wo_product_detail
#             (shipment data not yet synced from Lenovo). These WOs have active
#             WO statuses (e.g. "Parts in Transit", "Order Released") but are
#             invisible to every other tab because all other tabs require an
#             INNER JOIN to a part line.  no_part_lines = 1 for these rows.
_IN_PREPARE_COLS = """
    s.work_order_id, s.serial_number, s.created_on,
    s.committed_delivery_date, s.actual_committed_onsite_date,
    s.case_desc, s.work_order_type, s.contact_name,
    s.customer, s.work_order_status, s.case_status,
    d.completion_date, d.closing_date,
    u.full_name AS tech_name,
    p.product                AS part_product,
    p.description            AS part_description,
    p.order_date             AS part_order_date,
    p.acceptance_date        AS part_acceptance_date,
    p.eta_parthold_backlog   AS part_eta_wh,
    p.soid                   AS part_soid,
    0                        AS no_part_lines,
    (SELECT COUNT(*)
     FROM wo_product_detail ph
     WHERE ph.work_order_id = s.work_order_id
       AND ph.wo_product_status = 'On Hold - Part Hold'
       AND LOWER(COALESCE(ph.wo_product_status,'')) NOT LIKE '%cancel%'
    )                        AS part_on_hold_count,
    (SELECT COUNT(*)
     FROM wo_product_detail pt
     WHERE pt.work_order_id = s.work_order_id
       AND LOWER(COALESCE(pt.wo_product_status,'')) NOT LIKE '%cancel%'
    )                        AS part_total_order_count,
    (SELECT COUNT(*)
     FROM wo_product_detail pw
     WHERE pw.work_order_id = s.work_order_id
       AND LOWER(COALESCE(pw.wo_product_status,'')) NOT LIKE '%cancel%'
       AND TRIM(COALESCE(pw.ship_pickup_time,'')) = ''
       AND TRIM(COALESCE(pw.shipment_date,''))    = ''
    )                        AS part_waiting_pickup_count
"""

# Columns for PATH B (zero part lines) — part columns are all NULL / 0
_IN_PREPARE_COLS_NO_PART = """
    s.work_order_id, s.serial_number, s.created_on,
    s.committed_delivery_date, s.actual_committed_onsite_date,
    s.case_desc, s.work_order_type, s.contact_name,
    s.customer, s.work_order_status, s.case_status,
    d.completion_date, d.closing_date,
    u.full_name AS tech_name,
    NULL AS part_product,
    NULL AS part_description,
    NULL AS part_order_date,
    NULL AS part_acceptance_date,
    NULL AS part_eta_wh,
    NULL AS part_soid,
    1    AS no_part_lines,
    0    AS part_on_hold_count,
    0    AS part_total_order_count,
    0    AS part_waiting_pickup_count
"""


def get_asp_in_prepare_page(
    search: str = "",
    page: int = 1,
    page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
    prepare_filter: str = "",
) -> dict:
    """
    In-Prepare Follow-Up — two paths combined via UNION ALL:

    PATH A: WOs still open that have at least one non-cancelled part row which
            has not yet been shipped and not yet POD'd.
            no_part_lines = 0.

    PATH B: WOs still open that have ZERO rows in wo_product_detail (shipment
            data not yet synced from Lenovo).  These WOs are otherwise invisible
            to every follow-up tab.  no_part_lines = 1.

    Excluded from both paths:
      - Closed / cancelled WOs
      - WOs with completion_date or closing_date filled in wo_details

    prepare_filter values:
      ''                — all (default, both paths)
      'part_on_hold'    — PATH A only, WO status contains 'part hold'
      'no_part'         — PATH B only (no part lines at all)
      'waiting_pickup'  — PATH A only, has a part order but no ship_pickup_time
                          or shipment_date, and NOT on part hold
    """
    conn   = get_db()

    # ── shared open-WO guard ─────────────────────────────────────────────────
    _open_guard = """
        LOWER(COALESCE(s.work_order_status,'')) NOT LIKE '%cancel%'
        AND LOWER(COALESCE(s.work_order_status,'')) NOT IN (
            'closed','completed','rma in progress',
            'unit returned to customer /awaiting for parts rma',
            'repair completed','ready for pickup'
        )
        AND COALESCE(d.completion_date,'') = ''
        AND COALESCE(d.closing_date,'')    = ''
    """

    # ── PATH A — has a pre-ship part line ────────────────────────────────────
    path_a = f"""
        SELECT {{cols}}
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        LEFT JOIN asp_users u ON u.tech_id = d.tech_id
        JOIN (
            SELECT work_order_id,
                   MAX(soid) AS latest_soid
            FROM wo_product_detail
            WHERE LOWER(COALESCE(wo_product_status,'')) NOT LIKE '%cancel%'
              AND TRIM(COALESCE(ship_pickup_time,''))  = ''
              AND TRIM(COALESCE(shipment_date,''))      = ''
              AND TRIM(COALESCE(ship_pou_pod_time,'')) = ''
              AND TRIM(COALESCE(delivery_date,''))     = ''
            GROUP BY work_order_id
        ) latest ON latest.work_order_id = s.work_order_id
        JOIN wo_product_detail p ON p.soid = latest.latest_soid
        WHERE {_open_guard}
    """

    # ── PATH B — zero part lines at all ──────────────────────────────────────
    path_b = f"""
        SELECT {{cols_no_part}}
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        LEFT JOIN asp_users u ON u.tech_id = d.tech_id
        WHERE {_open_guard}
          AND NOT EXISTS (
              SELECT 1 FROM wo_product_detail p2
              WHERE p2.work_order_id = s.work_order_id
          )
    """

    def _build_search(params: list) -> str:
        if not search:
            return ""
        term = f"%{search.lower()}%"
        params.extend([term, term, term, term, term])
        return """
          AND (
            CAST(s.work_order_id AS TEXT) LIKE ?
            OR LOWER(s.serial_number)     LIKE ?
            OR LOWER(s.contact_name)      LIKE ?
            OR LOWER(s.customer)          LIKE ?
            OR LOWER(s.case_desc)         LIKE ?
          )
        """

    def _build_vendor(params: list) -> str:
        if tech_id_filter:
            params.append(tech_id_filter)
            return " AND d.tech_id = ?"
        if not vendor_filter:
            return ""
        params.append(vendor_filter)
        return " AND d.labor_vendor_related = ?"

    # ── sub-tab filter clauses ────────────────────────────────────────────────
    # Extra WHERE appended to PATH A or PATH B depending on the filter.
    # path_a_extra / path_b_extra are appended after search+vendor clauses.
    if prepare_filter == "part_on_hold":
        # PATH A only — WO status contains "part hold"
        path_a_extra = " AND LOWER(COALESCE(s.work_order_status,'')) LIKE '%part%hold%'"
        path_b_extra = None   # exclude PATH B entirely
    elif prepare_filter == "no_part":
        # PATH B only
        path_a_extra = None   # exclude PATH A entirely
        path_b_extra = ""
    elif prepare_filter == "waiting_pickup":
        # PATH A only — has a part line, NOT on part hold.
        # PATH A already guarantees ship_pickup_time and shipment_date are empty,
        # so every non-hold PATH A row is waiting for pickup regardless of order_date.
        path_a_extra = (
            " AND LOWER(COALESCE(s.work_order_status,'')) NOT LIKE '%part%hold%'"
        )
        path_b_extra = None   # exclude PATH B entirely
    else:
        path_a_extra = ""
        path_b_extra = ""

    def _union(col_a: str, col_b: str) -> str:
        """Build the UNION ALL SQL for count or rows, respecting active paths."""
        parts = []
        if path_a_extra is not None:
            parts.append(f"{path_a.format(cols=col_a)} {{sc_a}} {{vc_a}}{path_a_extra}")
        if path_b_extra is not None:
            parts.append(f"{path_b.format(cols_no_part=col_b)} {{sc_b}} {{vc_b}}{path_b_extra}")
        return " UNION ALL ".join(parts) if parts else "SELECT NULL WHERE 0"

    # ── COUNT — sum both paths ───────────────────────────────────────────────
    params_cnt: list = []
    sc_a  = _build_search(params_cnt) if path_a_extra is not None else ""
    vc_a  = _build_vendor(params_cnt) if path_a_extra is not None else ""
    sc_b  = _build_search(params_cnt) if path_b_extra is not None else ""
    vc_b  = _build_vendor(params_cnt) if path_b_extra is not None else ""

    union_cnt = _union('s.work_order_id', 's.work_order_id').format(
        sc_a=sc_a, vc_a=vc_a, sc_b=sc_b, vc_b=vc_b)
    count_sql = f"SELECT COUNT(*) FROM ({union_cnt})"
    total  = conn.execute(count_sql, params_cnt).fetchone()[0]
    pages  = max(1, -(-total // page_size))
    offset = (max(1, page) - 1) * page_size

    # ── ROWS — both paths, ordered newest-first, paginated ──────────────────
    params_rows: list = []
    sc_a2 = _build_search(params_rows) if path_a_extra is not None else ""
    vc_a2 = _build_vendor(params_rows) if path_a_extra is not None else ""
    sc_b2 = _build_search(params_rows) if path_b_extra is not None else ""
    vc_b2 = _build_vendor(params_rows) if path_b_extra is not None else ""

    union_rows = _union(_IN_PREPARE_COLS, _IN_PREPARE_COLS_NO_PART).format(
        sc_a=sc_a2, vc_a=vc_a2, sc_b=sc_b2, vc_b=vc_b2)
    if prepare_filter == "part_on_hold":
        order_by = """
            CASE WHEN TRIM(COALESCE(part_eta_wh,'')) = '' THEN 1 ELSE 0 END ASC,
            part_eta_wh ASC,
            created_on  ASC
        """
    else:
        order_by = "created_on DESC"

    rows_sql = f"""
        SELECT * FROM ({union_rows})
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(rows_sql, params_rows + [page_size, offset]).fetchall()

    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "pages": pages}


# Completed Last 30 Days — WOs with completion_date within the past 30 days
def get_asp_completed_history(
    days: int = 7,
    search: str = "",
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    Return completed/closed WOs whose closing_date or completion_date falls
    within the last `days` calendar days (WIB / UTC+7).
    Ordered by closing_date DESC, completion_date DESC.
    No pagination — callers pass days ≤ 30 so the result set stays small.
    """
    import datetime
    conn = get_db()

    days    = max(1, min(days, 30))
    now_wib = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    cutoff  = (now_wib - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

    params: list = [cutoff, cutoff]
    wheres: list[str] = [
        # at least one of closing_date / completion_date must be filled and within range
        """(
            (TRIM(COALESCE(d.closing_date,'')) != ''    AND SUBSTR(d.closing_date,1,10)    >= ?)
         OR (TRIM(COALESCE(d.completion_date,'')) != '' AND SUBSTR(d.completion_date,1,10) >= ?)
        )""",
    ]

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(s.work_order_id AS TEXT) LIKE ?
            OR LOWER(s.serial_number)     LIKE ?
            OR LOWER(s.contact_name)      LIKE ?
            OR LOWER(s.customer)          LIKE ?
            OR LOWER(s.case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if tech_id_filter:
        wheres.append("d.tech_id = ?")
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append("d.labor_vendor_related = ?")
        params.append(vendor_filter)

    where_sql = "WHERE " + " AND ".join(wheres)

    rows = conn.execute(f"""
        SELECT
            s.work_order_id,
            s.serial_number,
            s.created_on,
            s.committed_delivery_date,
            s.actual_committed_onsite_date,
            s.work_order_type,
            s.case_desc,
            s.work_order_status,
            s.case_status,
            s.contact_name,
            s.customer,
            d.completion_date,
            d.closing_date,
            d.city,
            d.mobile_phone,
            d.primary_email,
            d.product_description,
            d.product_id_mtm,
            u.full_name AS tech_name
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        LEFT JOIN asp_users u ON u.tech_id = d.tech_id
        {where_sql}
        ORDER BY
            MAX(COALESCE(d.closing_date,''), COALESCE(d.completion_date,'')) DESC,
            s.work_order_id DESC
    """, params).fetchall()

    result = [dict(r) for r in rows]
    return {"rows": result, "total": len(result), "days": days}


def get_asp_completed_last_30_days(
    search: str = "",
    type_filter: str = "",
    no_awb: bool = False,
    page: int = 1,
    page_size: int = 25,
    vendor_filter: str | None = None,
    tech_id_filter: str | None = None,
) -> dict:
    """
    Return WOs where wo_details.completion_date falls within the last 30 calendar days
    (WIB / UTC+7).  Ordered by completion_date DESC (most recently completed first).
    """
    import datetime
    conn = get_db()

    # WIB "today" cutoff: 30 days ago at 00:00:00 WIB
    now_wib  = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    cutoff   = (now_wib - datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    params: list = [cutoff]
    wheres: list[str] = [
        "TRIM(COALESCE(d.completion_date,'')) != ''",
        "SUBSTR(d.completion_date,1,10) >= ?",
    ]

    if search:
        term = f"%{search.lower()}%"
        wheres.append("""(
            CAST(s.work_order_id AS TEXT) LIKE ?
            OR LOWER(s.serial_number)     LIKE ?
            OR LOWER(s.contact_name)      LIKE ?
            OR LOWER(s.customer)          LIKE ?
            OR LOWER(s.case_desc)         LIKE ?
        )""")
        params.extend([term, term, term, term, term])

    if type_filter:
        wheres.append("LOWER(s.work_order_type) LIKE ?")
        params.append(f"%{type_filter.lower()}%")

    if no_awb:
        # Keep only WOs that have at least one active (non-cancelled, ordered) part
        # line with an empty AWB.
        wheres.append("""EXISTS (
            SELECT 1 FROM wo_product_detail p
            WHERE p.work_order_id = s.work_order_id
              AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
              AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
              AND TRIM(COALESCE(p.awb,'')) = ''
        )""")

    if tech_id_filter:
        wheres.append("d.tech_id = ?")
        params.append(tech_id_filter)
    elif vendor_filter:
        wheres.append("d.labor_vendor_related = ?")
        params.append(vendor_filter)

    where_sql = "WHERE " + " AND ".join(wheres)

    base_sql = f"""
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        {where_sql}
    """

    total  = conn.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()[0]
    pages  = max(1, -(-total // page_size))
    offset = (max(1, page) - 1) * page_size

    rows = conn.execute(f"""
        SELECT
            s.work_order_id, s.serial_number, s.created_on,
            s.work_order_type, s.case_desc,
            s.work_order_status, s.case_status,
            s.contact_name, s.customer,
            d.completion_date,
            d.closing_date,
            d.case_number,
            (SELECT COUNT(DISTINCT TRIM(p.awb))
             FROM wo_product_detail p
             WHERE p.work_order_id = s.work_order_id
               AND TRIM(COALESCE(p.awb,'')) != '') AS awb_count,
            (SELECT GROUP_CONCAT(DISTINCT TRIM(p.awb))
             FROM wo_product_detail p
             WHERE p.work_order_id = s.work_order_id
               AND TRIM(COALESCE(p.awb,'')) != '') AS awb_list,
            (SELECT COUNT(*)
             FROM wo_product_detail p
             WHERE p.work_order_id = s.work_order_id
               AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
               AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
               AND TRIM(COALESCE(p.awb,'')) = '') AS awb_missing_count
        {base_sql}
        ORDER BY d.completion_date DESC
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "pages": pages}


def get_pou_unreturn_report() -> list[dict]:
    """Return all wo_product_detail rows where is_exist_excel = 'yes',
    joined with wo_details for completion_date / closing_date.

    Columns returned:
      soid, completion_date, closing_date,
      return_status    — from wo_product_detail.return_status (GTAAP)
      dc_number        — from wo_product_detail.dc_number (GTAAP)
      dc_lenovo        — fallback DC from POU Unreturn upload
      lenovo_return_status
      awb_return       — AWB Number from POU Unreturn
      awb_notes        — Note from POU Unreturn
      is_exist_excel   — whether SOID is present in the uploaded Excel
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            p.soid,
            d.completion_date,
            d.closing_date,
            p.return_status,
            p.dc_number,
            p.dc_lenovo,
            p.lenovo_return_status,
            p.awb_return,
            p.awb_notes,
            p.is_exist_excel
        FROM wo_product_detail p
        LEFT JOIN wo_details d USING (work_order_id)
        WHERE p.is_exist_excel = 'yes'
        ORDER BY p.soid DESC
    """).fetchall()
    return [dict(r) for r in rows]
