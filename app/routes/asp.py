import os
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, send_file, current_app, session, redirect, url_for
from app.services.database.queries import get_wo_summary_stats
from app.routes.auth import login_required

asp_bp = Blueprint("asp", __name__)


# ── Session helper ────────────────────────────────────────────────────────────

def _vendor_filter() -> str | None:
    """Return the labor_vendor_related value for the current ASP session, or None."""
    if session.get("role") == "asp":
        return session.get("labor_vendor") or None
    return None


# ── Shared stat context ───────────────────────────────────────────────────────

def _stat_ctx() -> dict:
    """Stat counts only — no row data loaded on page request."""
    vf = _vendor_filter()
    s = get_wo_summary_stats(vendor_filter=vf)
    return dict(
        total              = s["total"],
        total_closed       = s["closed"],
        total_open         = s["open"],
        total_part_hold    = s["part_hold"],
        total_part_transit = s["part_transit"],
        portal             = "asp",
    )


# ── Page routes ───────────────────────────────────────────────────────────────

@asp_bp.route("/asp/dashboard", methods=["GET"])
@login_required
def dashboard():
    ctx = _stat_ctx()
    ctx["active_page"] = "asp_dashboard"
    return render_template("asp/dashboard.html", **ctx)


@asp_bp.route("/asp/work-orders", methods=["GET"])
@login_required
def work_orders():
    ctx = _stat_ctx()
    tab = request.args.get("tab", "active")
    ctx["active_page"]  = {"active": "wo_active", "closed": "wo_closed",
                           "escalated": "wo_escalated", "pending": "wo_pending"}.get(tab, "wo_active")
    ctx["active_group"] = "work_orders"
    return render_template("asp/work_orders.html", **ctx)


@asp_bp.route("/asp/parts", methods=["GET"])
@login_required
def parts_management():
    ctx = _stat_ctx()
    tab = request.args.get("tab", "awaiting")
    ctx["active_page"]  = {"awaiting": "parts_awaiting", "received": "parts_received",
                            "return": "parts_return"}.get(tab, "parts_awaiting")
    ctx["active_group"] = "parts"
    return render_template("asp/parts_management.html", **ctx)


@asp_bp.route("/asp/reschedule", methods=["GET"])
@login_required
def reschedule():
    ctx = _stat_ctx()
    ctx["active_page"] = "reschedule"
    return render_template("asp/reschedule.html", **ctx)


@asp_bp.route("/asp/escalation", methods=["GET"])
@login_required
def escalation():
    ctx = _stat_ctx()
    ctx["active_page"] = "escalation"
    return render_template("asp/escalation.html", **ctx)


# ── API: tab data endpoints (server-side pagination) ─────────────────────────

def _int_arg(name: str, default: int) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except (ValueError, TypeError):
        return default


@asp_bp.route("/asp/api/all-wo", methods=["GET"])
@login_required
def api_all_wo():
    """All WO tab — filterable by status, WO type, and case status."""
    from app.services.database.queries import get_asp_all_wo_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_all_wo_page(
        search              = request.args.get("q", "").strip(),
        status_filter       = request.args.get("status", "").strip(),
        type_filter         = request.args.get("wo_type", "").strip(),
        case_status_filter  = request.args.get("case_status", "").strip(),
        page                = _int_arg("page", 1),
        page_size           = per_page,
        vendor_filter       = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/part-received", methods=["GET"])
@login_required
def api_part_received():
    """Part Received tab — WOs waiting for part / on part hold."""
    from app.services.database.queries import get_asp_part_received_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_received_page(
        search        = request.args.get("q", "").strip(),
        page          = _int_arg("page", 1),
        page_size     = per_page,
        vendor_filter = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/cci-followup", methods=["GET"])
@login_required
def api_cci_followup():
    """CCI Follow-Up tab — all Carry-In WOs with computed followup_state."""
    from app.services.database.queries import get_asp_cci_followup_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_cci_followup_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/part-return", methods=["GET"])
@login_required
def api_part_return():
    """Part Return tab — closed / completed WOs."""
    from app.services.database.queries import get_asp_part_return_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_return_page(
        search        = request.args.get("q", "").strip(),
        page          = _int_arg("page", 1),
        page_size     = per_page,
        vendor_filter = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/reschedule", methods=["GET"])
@login_required
def api_reschedule():
    """WO Reschedule tab — open WOs eligible for rescheduling."""
    from app.services.database.queries import get_asp_reschedule_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_reschedule_page(
        search        = request.args.get("q", "").strip(),
        page          = _int_arg("page", 1),
        page_size     = per_page,
        vendor_filter = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/onsite-followup", methods=["GET"])
@login_required
def api_onsite_followup():
    """Onsite Follow-Up tab — all Onsite WOs with computed followup_state."""
    from app.services.database.queries import get_asp_onsite_followup_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_onsite_followup_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/wos-by-awb", methods=["GET"])
@login_required
def api_wos_by_awb():
    """Return all WOs sharing the given AWB number."""
    from app.services.database.queries import get_wos_by_awb
    awb = request.args.get("awb", "").strip()
    if not awb:
        return jsonify([])
    return jsonify(get_wos_by_awb(awb))


@asp_bp.route("/asp/api/wo-no-awb", methods=["GET"])
@login_required
def api_wo_no_awb():
    """Return open WOs for a given ASP (customer name) that have part lines with no AWB.
    If current_wo is supplied, that WO is always included even if it already has an AWB."""
    from app.services.database.queries import get_wo_no_awb_by_asp
    customer = request.args.get("customer", "").strip()
    if not customer:
        return jsonify([])
    current_wo = request.args.get("current_wo", "").strip()
    try:
        current_wo_id = int(current_wo) if current_wo else None
    except ValueError:
        current_wo_id = None
    return jsonify(get_wo_no_awb_by_asp(customer, current_wo_id=current_wo_id))


@asp_bp.route("/asp/api/return-part-same-asp", methods=["GET"])
@login_required
def api_return_part_same_asp():
    """Return closed WOs for a given ASP that have return_flag=Y parts with no DC number yet."""
    from app.services.database.queries import get_return_part_wos_by_asp
    customer = request.args.get("customer", "").strip()
    if not customer:
        return jsonify([])
    return jsonify(get_return_part_wos_by_asp(customer))


@asp_bp.route("/asp/api/wo-detail/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_detail(work_order_id: int):
    """Single WO full detail — wo_summary + wo_details joined."""
    from app.services.database.queries import get_wo_detail
    row = get_wo_detail(work_order_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    # ASP users: deny access to WOs outside their vendor scope
    vf = _vendor_filter()
    if vf and row.get("labor_vendor_related") != vf:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)


@asp_bp.route("/asp/api/wo-parts/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_parts(work_order_id: int):
    """All part-order lines for one WO from wo_product_detail."""
    from app.services.database.queries import get_parts_for_wo, get_wo_detail
    vf = _vendor_filter()
    if vf:
        row = get_wo_detail(work_order_id)
        if not row or row.get("labor_vendor_related") != vf:
            return jsonify([])
    rows = get_parts_for_wo(work_order_id)
    return jsonify(rows)


@asp_bp.route("/asp/api/wo-related-serial/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_related_serial(work_order_id: int):
    """Return all WOs (including the current one) that share the same serial_number."""
    from app.services.database.queries import get_wo_detail, get_wo_by_serial
    detail = get_wo_detail(work_order_id)
    vf = _vendor_filter()
    # Only gate access to the current WO; history rows are unfiltered context.
    if vf and (not detail or detail.get("labor_vendor_related") != vf):
        return jsonify({"serial_number": None, "current_wo_id": work_order_id, "rows": []})
    if not detail or not detail.get("serial_number"):
        return jsonify({"serial_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_serial(detail["serial_number"])
    return jsonify({"serial_number": detail["serial_number"], "current_wo_id": work_order_id, "rows": rows})


@asp_bp.route("/asp/api/wo-ticket-history/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_ticket_history(work_order_id: int):
    """Return all WOs (including the current one) that share the same case_number (ticket)."""
    from app.services.database.queries import get_wo_detail, get_wo_by_case_number
    detail = get_wo_detail(work_order_id)
    vf = _vendor_filter()
    # Only gate access to the current WO; history rows are unfiltered context.
    if vf and (not detail or detail.get("labor_vendor_related") != vf):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    if not detail or not detail.get("case_number"):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_case_number(detail["case_number"])
    return jsonify({"case_number": detail["case_number"], "current_wo_id": work_order_id, "rows": rows})


@asp_bp.route("/asp/api/working-hours", methods=["POST"])
@login_required
def api_save_working_hours():
    """Save updated working_hours string for the logged-in ASP user."""
    from app.services.database.db import get_db
    uid  = session.get("user_id")
    role = session.get("role", "")
    if role != "asp":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    working_hours = (data.get("working_hours") or "").strip()
    if not working_hours:
        return jsonify({"error": "working_hours is required"}), 400
    get_db().execute(
        "UPDATE asp_details SET working_hours = ? WHERE id = ?",
        (working_hours, uid)
    )
    get_db().commit()
    return jsonify({"ok": True, "working_hours": working_hours})


@asp_bp.route("/asp/api/in-prepare", methods=["GET"])
@login_required
def api_in_prepare():
    """In-Prepare Follow-Up — WOs with part ordered but not yet shipped."""
    from app.services.database.queries import get_asp_in_prepare_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_in_prepare_page(
        search        = request.args.get("q", "").strip(),
        page          = _int_arg("page", 1),
        page_size     = per_page,
        vendor_filter = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/in-delivery", methods=["GET"])
@login_required
def api_in_delivery():
    """In-Delivery Follow-Up — WOs with part shipped but not yet POD'd."""
    from app.services.database.queries import get_asp_in_delivery_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_in_delivery_page(
        search        = request.args.get("q", "").strip(),
        page          = _int_arg("page", 1),
        page_size     = per_page,
        vendor_filter = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/in-repair", methods=["GET"])
@login_required
def api_in_repair():
    """In-Repair Follow-Up — WOs with part POD'd but WO still open."""
    from app.services.database.queries import get_asp_in_repair_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_in_repair_page(
        search        = request.args.get("q", "").strip(),
        page          = _int_arg("page", 1),
        page_size     = per_page,
        vendor_filter = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/return-part", methods=["GET"])
@login_required
def api_return_part():
    """Return Part Follow-Up — closed/completed WOs with computed input_dc / return_part state."""
    from app.services.database.queries import get_asp_part_return_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_return_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
    ))


@asp_bp.route("/asp/api/return-part/export", methods=["GET"])
@login_required
def api_return_part_export():
    """Export Return Part Follow-Up rows expanded by SOID (one row per part line)
    for every WO that exists on the Return Part tab (followup_state=input_dc).
    Saves to files/report/ and streams the file back as a download."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from app.services.database.queries import get_asp_part_return_page
    from app.services.database.db import get_db

    # ── 1. Collect WOs in the Return Part tab ─────────────────────────────────
    result = get_asp_part_return_page(
        search         = request.args.get("q", "").strip(),
        followup_state = "input_dc",
        page           = 1,
        page_size      = 9999,
        vendor_filter  = _vendor_filter(),
    )
    wo_rows = result.get("rows", [])

    # Build a fast lookup: work_order_id → WO summary dict
    wo_map = {r["work_order_id"]: r for r in wo_rows}
    wo_ids = list(wo_map.keys())

    # ── 2. Fetch all SOID lines for those WOs ────────────────────────────────
    part_rows = []
    if wo_ids:
        conn = get_db()
        placeholders = ",".join("?" * len(wo_ids))
        part_rows = conn.execute(
            f"""
            SELECT
                p.work_order_id,
                p.soid,
                p.product,
                p.description,
                p.order_date,
                p.delivery_date,
                p.wo_product_status,
                p.return_flag,
                p.awb,
                p.ship_pou_pod_time,
                p.dc_number
            FROM wo_product_detail p
            WHERE p.work_order_id IN ({placeholders})
              AND LOWER(COALESCE(p.wo_product_status,'')) NOT LIKE '%cancel%'
              AND TRIM(COALESCE(p.order_date, p.acceptance_date,'')) != ''
              AND UPPER(TRIM(COALESCE(p.return_flag,''))) = 'Y'
            ORDER BY p.work_order_id, p.soid
            """,
            wo_ids,
        ).fetchall()
        part_rows = [dict(r) for r in part_rows]

    # ── 3. Build export rows: one row per SOID ────────────────────────────────
    def _fmt_date(val):
        if not val:
            return ""
        s = str(val).strip()
        return s[:10] if len(s) >= 10 else s

    export_rows = []
    for p in part_rows:
        wo = wo_map.get(p["work_order_id"], {})
        export_rows.append({
            "work_order_id":                wo.get("work_order_id", ""),
            "created_on":                   _fmt_date(wo.get("created_on")),
            "work_order_type":              wo.get("work_order_type", ""),
            "case_desc":                    wo.get("case_desc", ""),
            "work_order_status":            wo.get("work_order_status", ""),
            "committed_delivery_date":      _fmt_date(wo.get("committed_delivery_date")),
            "actual_committed_onsite_date": _fmt_date(wo.get("actual_committed_onsite_date")),
            "contact_name":                 wo.get("contact_name", ""),
            "customer":                     wo.get("customer", ""),
            "soid":                         (str(int(p["soid"])) if p.get("soid") is not None else ""),
            "product":                      p.get("product", ""),
            "description":                  p.get("description", ""),
            "order_date":                   _fmt_date(p.get("order_date")),
            "delivery_date":                _fmt_date(p.get("delivery_date")),
            "wo_product_status":            p.get("wo_product_status", ""),
            "return_flag":                  p.get("return_flag", ""),
            "awb":                          p.get("awb", ""),
            "ship_pou_pod_time":            _fmt_date(p.get("ship_pou_pod_time")),
            "dc_number":                    p.get("dc_number", ""),
        })

    # ── 4. Build workbook ─────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Return Part - By SOID"

    headers = [
        "No.", "WO Number", "Created On", "WO Type", "Case",
        "WO Status", "Committed Delivery", "Actual Committed",
        "Contact Name", "ASP",
        "SOID", "Part Number", "Description", "Order Date", "Delivery Date",
        "Part Status", "Return Flag", "AWB", "POD Date", "DC Number",
    ]
    col_keys = [
        None,
        "work_order_id", "created_on", "work_order_type", "case_desc",
        "work_order_status", "committed_delivery_date", "actual_committed_onsite_date",
        "contact_name", "customer",
        "soid", "product", "description", "order_date", "delivery_date",
        "wo_product_status", "return_flag", "awb", "ship_pou_pod_time", "dc_number",
    ]
    col_widths = [6, 16, 16, 14, 32, 22, 20, 20, 22, 28, 14, 16, 30, 14, 14, 22, 12, 18, 14, 14]

    # Header style
    hdr_fill   = PatternFill("solid", fgColor="1F2328")
    hdr_font   = Font(bold=True, color="FFFFFF", size=11)
    hdr_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side  = Side(style="thin", color="E5E7EB")
    thin_border = Border(left=thin_side, right=thin_side, bottom=thin_side, top=thin_side)

    for ci, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill      = hdr_fill
        cell.font      = hdr_font
        cell.alignment = hdr_align
        cell.border    = thin_border
        ws.column_dimensions[cell.column_letter].width = w

    ws.row_dimensions[1].height = 22

    # Row styles
    even_fill  = PatternFill("solid", fgColor="F7F8FA")
    data_font  = Font(size=11)
    data_align = Alignment(vertical="center")

    for ri, r in enumerate(export_rows, start=2):
        fill = even_fill if ri % 2 == 0 else PatternFill()
        for ci, key in enumerate(col_keys, start=1):
            value = (ri - 1) if key is None else (r.get(key) or "")
            cell = ws.cell(row=ri, column=ci, value=value)
            cell.font      = data_font
            cell.alignment = data_align
            cell.border    = thin_border
            if key == "soid":
                cell.number_format = "@"
            if fill.fill_type:
                cell.fill = fill
        ws.row_dimensions[ri].height = 18

    # Freeze header row
    ws.freeze_panes = "A2"

    # ── 5. Save and stream ────────────────────────────────────────────────────
    report_dir = current_app.config["REPORT_DIR"]
    os.makedirs(report_dir, exist_ok=True)
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename   = f"Return_Part_BySoid_{timestamp}.xlsx"
    filepath   = os.path.join(report_dir, filename)
    wb.save(filepath)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
