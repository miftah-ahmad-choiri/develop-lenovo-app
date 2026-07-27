from flask import Blueprint, render_template, request, jsonify
from app.services.database.queries import get_wo_summary_stats

asp_bp = Blueprint("asp", __name__)


# ── Shared stat context ───────────────────────────────────────────────────────

def _stat_ctx() -> dict:
    """Stat counts only — no row data loaded on page request."""
    s = get_wo_summary_stats()
    return dict(
        total                = s["total"],
        total_closed         = s["closed"],
        total_open           = s["open"],
        total_part_hold      = s["part_hold"],
        total_part_transit   = s["part_transit"],
        portal               = "asp",
    )


# ── Page routes ───────────────────────────────────────────────────────────────

@asp_bp.route("/asp/dashboard", methods=["GET"])
def dashboard():
    ctx = _stat_ctx()
    ctx["active_page"] = "asp_dashboard"
    return render_template("asp/dashboard.html", **ctx)


@asp_bp.route("/asp/work-orders", methods=["GET"])
def work_orders():
    ctx = _stat_ctx()
    tab = request.args.get("tab", "active")
    ctx["active_page"]  = {"active": "wo_active", "closed": "wo_closed",
                           "escalated": "wo_escalated", "pending": "wo_pending"}.get(tab, "wo_active")
    ctx["active_group"] = "work_orders"
    return render_template("asp/work_orders.html", **ctx)


@asp_bp.route("/asp/parts", methods=["GET"])
def parts_management():
    ctx = _stat_ctx()
    tab = request.args.get("tab", "awaiting")
    ctx["active_page"]  = {"awaiting": "parts_awaiting", "received": "parts_received",
                           "return": "parts_return"}.get(tab, "parts_awaiting")
    ctx["active_group"] = "parts"
    return render_template("asp/parts_management.html", **ctx)


@asp_bp.route("/asp/reschedule", methods=["GET"])
def reschedule():
    ctx = _stat_ctx()
    ctx["active_page"] = "reschedule"
    return render_template("asp/reschedule.html", **ctx)


@asp_bp.route("/asp/escalation", methods=["GET"])
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
    ))


@asp_bp.route("/asp/api/part-received", methods=["GET"])
def api_part_received():
    """Part Received tab — WOs waiting for part / on part hold."""
    from app.services.database.queries import get_asp_part_received_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_received_page(
        search    = request.args.get("q", "").strip(),
        page      = _int_arg("page", 1),
        page_size = per_page,
    ))


@asp_bp.route("/asp/api/cci-followup", methods=["GET"])
def api_cci_followup():
    """CCI Follow-Up tab — all Carry-In WOs with computed followup_state."""
    from app.services.database.queries import get_asp_cci_followup_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_cci_followup_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
    ))


@asp_bp.route("/asp/api/part-return", methods=["GET"])
def api_part_return():
    """Part Return tab — closed / completed WOs."""
    from app.services.database.queries import get_asp_part_return_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_return_page(
        search    = request.args.get("q", "").strip(),
        page      = _int_arg("page", 1),
        page_size = per_page,
    ))


@asp_bp.route("/asp/api/reschedule", methods=["GET"])
def api_reschedule():
    """WO Reschedule tab — open WOs eligible for rescheduling."""
    from app.services.database.queries import get_asp_reschedule_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_reschedule_page(
        search    = request.args.get("q", "").strip(),
        page      = _int_arg("page", 1),
        page_size = per_page,
    ))


@asp_bp.route("/asp/api/onsite-followup", methods=["GET"])
def api_onsite_followup():
    """Onsite Follow-Up tab — all Onsite WOs with computed followup_state."""
    from app.services.database.queries import get_asp_onsite_followup_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_onsite_followup_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
    ))


@asp_bp.route("/asp/api/wos-by-awb", methods=["GET"])
def api_wos_by_awb():
    """Return all WOs sharing the given AWB number."""
    from app.services.database.queries import get_wos_by_awb
    awb = request.args.get("awb", "").strip()
    if not awb:
        return jsonify([])
    return jsonify(get_wos_by_awb(awb))


@asp_bp.route("/asp/api/wo-no-awb", methods=["GET"])
def api_wo_no_awb():
    """Return open WOs for a given ASP (customer name) that have part lines with no AWB."""
    from app.services.database.queries import get_wo_no_awb_by_asp
    customer = request.args.get("customer", "").strip()
    if not customer:
        return jsonify([])
    return jsonify(get_wo_no_awb_by_asp(customer))


@asp_bp.route("/asp/api/wo-detail/<int:work_order_id>", methods=["GET"])
def api_wo_detail(work_order_id: int):
    """Single WO full detail — wo_summary + wo_details joined."""
    from app.services.database.queries import get_wo_detail
    row = get_wo_detail(work_order_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)


@asp_bp.route("/asp/api/wo-parts/<int:work_order_id>", methods=["GET"])
def api_wo_parts(work_order_id: int):
    """All part-order lines for one WO from wo_product_detail."""
    from app.services.database.queries import get_parts_for_wo
    rows = get_parts_for_wo(work_order_id)
    return jsonify(rows)


@asp_bp.route("/asp/api/wo-related-serial/<int:work_order_id>", methods=["GET"])
def api_wo_related_serial(work_order_id: int):
    """Return all WOs (including the current one) that share the same serial_number."""
    from app.services.database.queries import get_wo_detail, get_wo_by_serial
    detail = get_wo_detail(work_order_id)
    if not detail or not detail.get("serial_number"):
        return jsonify({"serial_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_serial(detail["serial_number"])
    return jsonify({"serial_number": detail["serial_number"], "current_wo_id": work_order_id, "rows": rows})


@asp_bp.route("/asp/api/wo-ticket-history/<int:work_order_id>", methods=["GET"])
def api_wo_ticket_history(work_order_id: int):
    """Return all WOs (including the current one) that share the same case_number (ticket)."""
    from app.services.database.queries import get_wo_detail, get_wo_by_case_number
    detail = get_wo_detail(work_order_id)
    if not detail or not detail.get("case_number"):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_case_number(detail["case_number"])
    return jsonify({"case_number": detail["case_number"], "current_wo_id": work_order_id, "rows": rows})
