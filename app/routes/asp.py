import os
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, send_file, current_app, session, redirect, url_for, flash
from app.services.database.queries import get_wo_summary_stats
from app.routes.auth import login_required

asp_bp = Blueprint("asp", __name__)


# ── Session helper ────────────────────────────────────────────────────────────

def _vendor_filter() -> str | None:
    """Return the labor_vendor_related value for the current ASP session, or None.

    asp_master: labor_vendor is None at login (they span the whole group), but
    may be scoped to a specific branch after switch_branch — so we still read
    the session value; it will be None (= unfiltered view) until a branch is chosen.
    asp_user: vendor filter is intentionally ignored — use _tech_id_filter() instead.
    """
    if session.get("role") in ("asp", "asp_master"):
        return session.get("labor_vendor") or None
    return None


# Sentinel used when an asp_user has no tech_id assigned.
# Any query that filters by this value will match zero rows because no real
# tech_id can equal this string — giving the user an empty result set rather
# than an unfiltered (all-vendor) view.
_NO_TECH_ID_SENTINEL = "__no_tech_id__"


def _tech_id_filter() -> str | None:
    """Return the tech_id for asp_user sessions only, or None for all other roles.

    When set, every WO query is narrowed to WOs assigned to this specific
    technician (wo_details.tech_id), so technicians cannot see each other's WOs.

    If the session role is asp_user but no tech_id is assigned, returns
    _NO_TECH_ID_SENTINEL so that ALL WO queries return zero rows — the user
    must have a tech_id to see any Work Orders.
    """
    if session.get("role") == "asp_user":
        tech_id = session.get("tech_id")
        return tech_id if tech_id else _NO_TECH_ID_SENTINEL
    return None


# ── Shared stat context ───────────────────────────────────────────────────────

def _has_tech_id() -> bool:
    """True for every role except asp_user-without-tech_id."""
    if session.get("role") == "asp_user":
        return bool(session.get("tech_id"))
    return True


def _stat_ctx() -> dict:
    """Stat counts only — no row data loaded on page request."""
    vf = _vendor_filter()
    tf = _tech_id_filter()
    s = get_wo_summary_stats(vendor_filter=vf, tech_id_filter=tf)
    return dict(
        total              = s["total"],
        total_closed       = s["closed"],
        total_open         = s["open"],
        total_part_hold    = s["part_hold"],
        total_part_transit = s["part_transit"],
        portal             = "asp",
        has_tech_id        = _has_tech_id(),
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


@asp_bp.route("/asp/switch-branch/<string:username>", methods=["GET"])
@login_required
def switch_branch(username: str):
    """Switch the current asp_master session context to a specific branch ASP.

    Only asp_master (or superadmin) may use this route.  Updates the session's
    labor_vendor and display_name so all pages filter correctly for the chosen
    branch.  Switching back to the master username (stored in original_username)
    clears the branch scope and returns to the unfiltered group view.
    """
    from app.services.database.db import get_db
    role         = session.get("role", "")
    own_username = session.get("username", "")

    if role not in ("superadmin", "asp_master"):
        flash("You do not have permission to switch offices.", "danger")
        return redirect(url_for("asp.dashboard"))

    db = get_db()

    # Fetch the target ASP
    target = db.execute(
        "SELECT username, service_provider, labor_vendor_related, parent_group, kota "
        "FROM asp_details WHERE username = ?",
        (username,),
    ).fetchone()

    if not target:
        flash("Branch office not found.", "danger")
        return redirect(url_for("asp.dashboard"))

    if role == "asp_master":
        # Verify the target belongs to this master's parent_group
        if target["parent_group"] != session.get("parent_group"):
            flash("You can only switch to offices in your own group.", "danger")
            return redirect(url_for("asp.dashboard"))

    # Preserve the original master identity on first switch
    if "original_username" not in session:
        session["original_username"]     = own_username
        session["original_display_name"] = session.get("display_name")
        session["original_labor_vendor"] = session.get("labor_vendor")
        session["original_office_kota"]  = session.get("office_kota", "")

    # Switching back to the master account: restore unscoped identity
    if username == session.get("original_username"):
        session["username"]     = session.pop("original_username")
        session["display_name"] = session.pop("original_display_name")
        session["labor_vendor"] = session.pop("original_labor_vendor")
        session["office_kota"]  = session.pop("original_office_kota", "")
    else:
        session["username"]     = target["username"]
        session["display_name"] = target["service_provider"] or target["username"]
        session["labor_vendor"] = target["labor_vendor_related"]
        session["office_kota"]  = target["kota"] or ""
        # is_hq_with_branches stays True — switcher must remain visible

    return redirect(url_for("asp.dashboard"))


@asp_bp.route("/asp/branch-office", methods=["GET"])
@login_required
def branch_office():
    """List all ASPs that share the same parent_group as the current asp_master user.
    Superadmin may pass ?parent_group=<name> to view any group."""
    from app.services.database.db import get_db
    role     = session.get("role", "")
    username = session.get("username", "")

    # Access control: only superadmin or asp_master
    if role not in ("superadmin", "asp_master"):
        flash("You do not have permission to access that page.", "danger")
        return redirect(url_for("asp.dashboard"))

    db = get_db()

    if role == "superadmin":
        # Superadmin may specify any parent_group via query string
        parent_group = request.args.get("parent_group", "").strip() or None
        current_asp  = None
    else:
        # asp_master: parent_group is stored directly in the session
        parent_group = session.get("parent_group")
        current_asp  = None

    if not parent_group:
        flash("No parent group configured for your account.", "warning")
        return redirect(url_for("asp.dashboard"))

    # Fetch all members of the group, HQ first then branches alphabetically
    members = db.execute(
        """
        SELECT username, service_provider, store_name, kota, island,
               vendor_code, labor_vendor_related, office_type,
               operational_status, operation_support, wo_count, phone_number
        FROM asp_details
        WHERE parent_group = ?
        ORDER BY
            CASE WHEN office_type = 'ASP HQ' THEN 0 ELSE 1 END,
            service_provider COLLATE NOCASE
        """,
        (parent_group,),
    ).fetchall()

    ctx = _stat_ctx()
    ctx.update(
        active_page  = "branch_office",
        parent_group = parent_group,
        members      = [dict(r) for r in members],
        current_asp  = current_asp,
    )
    return render_template("asp/branch_office.html", **ctx)


# ── API: tab data endpoints (server-side pagination) ─────────────────────────

def _int_arg(name: str, default: int) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except (ValueError, TypeError):
        return default


def _bool_arg(name: str, default: bool) -> bool:
    value = request.args.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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
        tech_id_filter      = _tech_id_filter(),
    ))


@asp_bp.route("/asp/api/part-received", methods=["GET"])
@login_required
def api_part_received():
    """Part Received tab — WOs waiting for part / on part hold."""
    from app.services.database.queries import get_asp_part_received_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_received_page(
        search         = request.args.get("q", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
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
        tech_id_filter = _tech_id_filter(),
    ))


@asp_bp.route("/asp/api/part-return", methods=["GET"])
@login_required
def api_part_return():
    """Part Return tab — closed / completed WOs."""
    from app.services.database.queries import get_asp_part_return_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_return_page(
        search         = request.args.get("q", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
    ))


@asp_bp.route("/asp/api/reschedule", methods=["GET"])
@login_required
def api_reschedule():
    """WO Reschedule tab — open WOs eligible for rescheduling."""
    from app.services.database.queries import get_asp_reschedule_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_reschedule_page(
        search         = request.args.get("q", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
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
        tech_id_filter = _tech_id_filter(),
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
    """Return closed WOs for a given ASP that have pending return_status (PENDING WITH PARTNER or PENDING FOR DC GENERATION)."""
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
    # asp_user: only their own assigned WOs
    tf = _tech_id_filter()
    if tf and row.get("tech_id") != tf:
        return jsonify({"error": "Not found"}), 404
    # asp / asp_master: vendor scope check
    vf = _vendor_filter()
    if vf and row.get("labor_vendor_related") != vf:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)


@asp_bp.route("/asp/api/wo-parts/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_parts(work_order_id: int):
    """All part-order lines for one WO from wo_product_detail."""
    from app.services.database.queries import get_parts_for_wo, get_wo_detail
    tf = _tech_id_filter()
    vf = _vendor_filter()
    if tf or vf:
        row = get_wo_detail(work_order_id)
        if tf and (not row or row.get("tech_id") != tf):
            return jsonify([])
        if vf and (not row or row.get("labor_vendor_related") != vf):
            return jsonify([])
    rows = get_parts_for_wo(work_order_id)
    return jsonify(rows)


@asp_bp.route("/asp/api/wo-related-serial/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_related_serial(work_order_id: int):
    """Return all WOs (including the current one) that share the same serial_number."""
    from app.services.database.queries import get_wo_detail, get_wo_by_serial
    detail = get_wo_detail(work_order_id)
    tf = _tech_id_filter()
    vf = _vendor_filter()
    # Only gate access to the current WO; history rows are unfiltered context.
    if tf and (not detail or detail.get("tech_id") != tf):
        return jsonify({"serial_number": None, "current_wo_id": work_order_id, "rows": []})
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
    tf = _tech_id_filter()
    vf = _vendor_filter()
    # Only gate access to the current WO; history rows are unfiltered context.
    if tf and (not detail or detail.get("tech_id") != tf):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    if vf and (not detail or detail.get("labor_vendor_related") != vf):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    if not detail or not detail.get("case_number"):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_case_number(detail["case_number"])
    return jsonify({"case_number": detail["case_number"], "current_wo_id": work_order_id, "rows": rows})


@asp_bp.route("/asp/api/wo-monday-escalation/<int:work_order_id>", methods=["GET"])
@login_required
def api_wo_monday_escalation(work_order_id: int):
    """Return all Monday technical_escalation rows that share the same serial_number as the WO."""
    import os as _os
    from app.services.database.queries import get_wo_detail
    from app.services.database.db import open_db

    detail = get_wo_detail(work_order_id)
    tf = _tech_id_filter()
    vf = _vendor_filter()
    if tf and (not detail or detail.get("tech_id") != tf):
        return jsonify({"serial_number": None, "rows": []})
    if vf and (not detail or detail.get("labor_vendor_related") != vf):
        return jsonify({"serial_number": None, "rows": []})
    if not detail or not detail.get("serial_number"):
        return jsonify({"serial_number": None, "rows": []})

    sn = detail["serial_number"].strip()

    project_root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
    db_path = _os.path.join(project_root, "files", "lenovo_asp_escalation.db")
    main_db_path = _os.path.join(project_root, "files", "lenovo_asp.db")
    if not _os.path.isfile(db_path):
        return jsonify({"serial_number": sn, "rows": []})

    edb = open_db(db_path)
    try:
        edb.execute(f"ATTACH DATABASE '{main_db_path}' AS main_db")
    except Exception:
        pass
    rows = []
    try:
        raw = edb.execute(
            """
            SELECT
                te.monday_item_id,
                te.board_id,
                te.asp_board,
                te.item_name,
                te.item_created_at,
                te.item_updated_at,
                te.status,
                te.work_order_type,
                te.wo_case_id,
                te.serial_number,
                te.ppsn_category,
                te.rrr_category,
                te.diag_datetime,
                te.diag_agent_ce,
                te.diag_model,
                te.diag_warranty,
                te.diag_problem,
                te.diag_esc_approval,
                te.diag_parts_request,
                te.diagnose_note,
                te.repair_note,
                (
                    SELECT COUNT(DISTINCT u2.update_id) + COUNT(DISTINCT r2.reply_id)
                    FROM item_updates u2
                    LEFT JOIN item_update_replies r2 ON u2.update_id = r2.update_id
                    WHERE u2.monday_item_id = te.monday_item_id
                ) AS disc_count,
                (
                    SELECT wd.case_number
                    FROM main_db.wo_details wd
                    WHERE CAST(wd.work_order_id AS TEXT) = TRIM(te.wo_case_id)
                    LIMIT 1
                ) AS case_number
            FROM technical_escalation te
            WHERE LOWER(TRIM(te.serial_number)) = LOWER(?)
            ORDER BY te.item_created_at ASC
            """,
            (sn,),
        ).fetchall()
        rows = [dict(r) for r in raw]
    except Exception as _e:
        current_app.logger.error("api_wo_monday_escalation query failed: %s", _e)
    finally:
        edb.close()

    return jsonify({"serial_number": sn, "rows": rows})


@asp_bp.route("/asp/api/sn-history/<path:serial_number>", methods=["GET"])
@login_required
def api_sn_history(serial_number: str):
    """Return all WOs in wo_summary/wo_details that share the given serial_number."""
    from app.services.database.queries import get_wo_by_serial
    rows = get_wo_by_serial(serial_number.strip())
    return jsonify({"serial_number": serial_number.strip(), "rows": rows})


@asp_bp.route("/asp/api/sn-monday-escalation/<path:serial_number>", methods=["GET"])
@login_required
def api_sn_monday_escalation(serial_number: str):
    """Return all Monday technical_escalation rows for a given serial number."""
    import os as _os
    sn = serial_number.strip()

    project_root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
    db_path      = _os.path.join(project_root, "files", "lenovo_asp_escalation.db")
    main_db_path = _os.path.join(project_root, "files", "lenovo_asp.db")
    if not _os.path.isfile(db_path):
        return jsonify({"serial_number": sn, "rows": []})

    from app.services.database.db import open_db
    edb = open_db(db_path)
    try:
        edb.execute(f"ATTACH DATABASE '{main_db_path}' AS main_db")
    except Exception:
        pass
    rows = []
    try:
        raw = edb.execute(
            """
            SELECT
                te.monday_item_id,
                te.board_id,
                te.asp_board,
                te.item_name,
                te.item_created_at,
                te.status,
                te.work_order_type,
                te.wo_case_id,
                te.serial_number,
                (
                    SELECT COUNT(DISTINCT u2.update_id) + COUNT(DISTINCT r2.reply_id)
                    FROM item_updates u2
                    LEFT JOIN item_update_replies r2 ON u2.update_id = r2.update_id
                    WHERE u2.monday_item_id = te.monday_item_id
                ) AS disc_count,
                (
                    SELECT wd.case_number
                    FROM main_db.wo_details wd
                    WHERE CAST(wd.work_order_id AS TEXT) = TRIM(te.wo_case_id)
                       OR CAST(COALESCE(wd.case_number, '') AS TEXT) = TRIM(te.wo_case_id)
                    LIMIT 1
                ) AS case_number
            FROM technical_escalation te
            WHERE LOWER(TRIM(te.serial_number)) = LOWER(?)
            ORDER BY te.item_created_at ASC
            """,
            (sn,),
        ).fetchall()
        rows = [dict(r) for r in raw]
    except Exception as _e:
        current_app.logger.error("api_sn_monday_escalation query failed: %s", _e)
    finally:
        edb.close()

    return jsonify({"serial_number": sn, "rows": rows})


@asp_bp.route("/asp/api/dashboard-closing-codes", methods=["GET"])
@login_required
def api_dashboard_closing_codes():
    """
    Return WOs whose completion_date falls within the last 30 days (WIB / UTC+7)
    and whose closing_code is one of the tracked follow-up / special-outcome codes,
    sorted by completion_date DESC. Exactly identical to Admin Dashboard.
    """
    import os as _os
    import datetime as _dt
    import re as _re
    from app.services.database.db import get_db, open_db

    _TRACKED_CODES = (
        "Need follow up \u2013 New Problem Found",
        "Needs Follow up - Wrong Part",
        "Needs Follow up - Dead on Arrival",
        "Need follow up \u2013 Parts Issue",
        "Need follow up \u2013 Others",
        "Need Follow up - Wrong Diagnosis",
        "Need Follow Up",
        "Customer Induced Damage",
        "Cannot recreate problem",
        "Parts replaced",
    )

    now_wib = _dt.datetime.utcnow() + _dt.timedelta(hours=7)
    cutoff  = (now_wib - _dt.timedelta(days=30)).strftime("%Y-%m-%d")

    placeholders = ",".join("?" * len(_TRACKED_CODES))
    params       = list(_TRACKED_CODES) + [cutoff]

    conn = get_db()
    project_root = _os.path.normpath(
        _os.path.join(_os.path.dirname(__file__), "..", "..")
    )
    esc_db_path  = _os.path.join(project_root, "files", "lenovo_asp_escalation.db")
    main_db_path = _os.path.join(project_root, "files", "lenovo_asp.db")
    edb = open_db(esc_db_path) if _os.path.isfile(esc_db_path) else None
    if edb:
        try:
            edb.execute(f"ATTACH DATABASE '{main_db_path}' AS main_db")
        except Exception:
            pass

    wo_rows = conn.execute(f"""
        SELECT
            s.work_order_id,
            s.serial_number,
            d.serial_number AS product_serial_number,
            s.work_order_type,
            s.work_order_status,
            s.customer,
            s.contact_name,
            d.product_description,
            d.city,
            d.completion_date,
            d.closing_date,
            d.closing_code,
            d.case_number,
            CAST(s.work_order_id AS TEXT)   AS wo_id_str,
            CAST(COALESCE(d.case_number, '') AS TEXT) AS case_num_str
        FROM wo_summary s
        LEFT JOIN wo_details d USING (work_order_id)
        WHERE d.closing_code IN ({placeholders})
          AND TRIM(COALESCE(d.completion_date, '')) != ''
          AND SUBSTR(d.completion_date, 1, 10) >= ?
        ORDER BY d.completion_date DESC
        LIMIT 200
    """, params).fetchall()

    wo_rows = [dict(r) for r in wo_rows]

    for r in wo_rows:
        r["row_source"] = "closing_code"

    monday_extra_rows = []

    if edb:
        try:
            disc_by_item: dict = {}
            esc_by_key: dict   = {}
            serial_esc: dict   = {}

            if wo_rows:
                keys = set()
                for r in wo_rows:
                    keys.add(r["wo_id_str"])
                    if r["case_num_str"]:
                        keys.add(r["case_num_str"])
                keys.discard("")
                key_list = list(keys)
                key_ph   = ",".join("?" * len(key_list))

                esc_agg = edb.execute(f"""
                    SELECT
                        TRIM(wo_case_id)                             AS key,
                        GROUP_CONCAT(DISTINCT COALESCE(status,'—')) AS statuses,
                        MAX(item_created_at)                         AS latest_created_at,
                        monday_item_id,
                        item_name,
                        board_id
                    FROM technical_escalation
                    WHERE TRIM(wo_case_id) IN ({key_ph})
                    GROUP BY TRIM(wo_case_id)
                """, key_list).fetchall()
                esc_by_key = {dict(r)["key"]: dict(r) for r in esc_agg}

                item_ids = [r["monday_item_id"] for r in esc_agg if r["monday_item_id"]]
                if item_ids:
                    item_ph    = ",".join("?" * len(item_ids))
                    _disc_rows = edb.execute(f"""
                        SELECT u.monday_item_id,
                               COUNT(DISTINCT u.update_id) + COUNT(DISTINCT r.reply_id) AS cnt
                        FROM item_updates u
                        LEFT JOIN item_update_replies r ON r.update_id = u.update_id
                        WHERE u.monday_item_id IN ({item_ph})
                        GROUP BY u.monday_item_id
                    """, item_ids).fetchall()
                    disc_by_item = {row["monday_item_id"]: row["cnt"] for row in _disc_rows}

                serial_esc = {}
                for esc_row in edb.execute("""
                    SELECT
                        TRIM(serial_number) AS serial_key,
                        monday_item_id,
                        item_name,
                        item_created_at,
                        status,
                        wo_case_id,
                        board_id
                    FROM technical_escalation
                    WHERE TRIM(COALESCE(serial_number, '')) != ''
                """).fetchall():
                    serial_esc.setdefault(esc_row["serial_key"].lower(), []).append(dict(esc_row))

            for r in wo_rows:
                wo_esc  = esc_by_key.get(r["wo_id_str"])
                case_esc = esc_by_key.get(r["case_num_str"])
                esc = wo_esc or case_esc
                match_type = "wo" if wo_esc else ("case" if case_esc else None)

                if not esc:
                    serial_key = (r["product_serial_number"] or r["serial_number"] or "").strip().lower()
                    completion_date = (r["completion_date"] or "")[:10]
                    candidates = []
                    if serial_key and completion_date:
                        try:
                            _comp_dt = _dt.date.fromisoformat(completion_date)
                        except ValueError:
                            _comp_dt = None
                        if _comp_dt:
                            for serial_row in serial_esc.get(serial_key, []):
                                escalation_date = (serial_row["item_created_at"] or "")[:10]
                                if escalation_date:
                                    try:
                                        _esc_dt = _dt.date.fromisoformat(escalation_date)
                                    except ValueError:
                                        continue
                                    delta = (_esc_dt - _comp_dt).days
                                    if -4 <= delta <= 7:
                                        candidates.append(serial_row)
                    if candidates:
                        esc = max(candidates, key=lambda item: item["item_created_at"] or "")
                        esc["statuses"] = ",".join(dict.fromkeys(
                            item["status"] or "—" for item in candidates
                        ))
                        esc["latest_created_at"] = max(
                            item["item_created_at"] or "" for item in candidates
                        )
                        match_type = "serial"
                if esc:
                    r["esc_statuses"]   = esc["statuses"]
                    r["esc_created_at"] = esc["latest_created_at"]
                    r["esc_item_id"]    = esc["monday_item_id"]
                    r["esc_item_name"]  = esc["item_name"]
                    r["esc_board_id"]   = esc["board_id"]
                    r["esc_disc_count"] = disc_by_item.get(esc["monday_item_id"], 0)
                    if match_type == "serial" and esc["monday_item_id"]:
                        disc_rows = edb.execute("""
                            SELECT COUNT(DISTINCT u.update_id) + COUNT(DISTINCT rep.reply_id) AS cnt
                            FROM item_updates u
                            LEFT JOIN item_update_replies rep ON rep.update_id = u.update_id
                            WHERE u.monday_item_id = ?
                        """, (esc["monday_item_id"],)).fetchone()
                        r["esc_disc_count"] = disc_rows["cnt"] if disc_rows else 0
                    r["wo_case_id"]     = (
                        esc.get("key") or esc.get("wo_case_id")
                        if match_type != "serial"
                        else f"SN: {r['product_serial_number'] or r['serial_number']}"
                    )
                    r["wo_case_match"]  = match_type
                else:
                    r["esc_statuses"]   = None
                    r["esc_created_at"] = None
                    r["esc_item_id"]    = None
                    r["esc_item_name"]  = None
                    r["esc_board_id"]   = None
                    r["esc_disc_count"] = 0
                    r["wo_case_id"]     = None
                    r["wo_case_match"]  = None

            matched_wo_ids  = {r["wo_id_str"] for r in wo_rows}
            matched_case_ids = {r["case_num_str"] for r in wo_rows if r["case_num_str"]}

            all_monday = edb.execute("""
                SELECT
                    monday_item_id,
                    item_name,
                    item_created_at,
                    status,
                    wo_case_id,
                    serial_number,
                    board_id,
                    work_order_type
                FROM technical_escalation
                WHERE TRIM(COALESCE(item_created_at, '')) != ''
                  AND SUBSTR(item_created_at, 1, 10) >= ?
                ORDER BY item_created_at DESC
            """, (cutoff,)).fetchall()

            closed_by_case: dict = {}
            all_closed_wo = conn.execute("""
                SELECT
                    CAST(s.work_order_id AS TEXT) AS wo_id_str,
                    d.completion_date,
                    d.closing_date,
                    s.work_order_type,
                    s.work_order_status,
                    d.case_number,
                    d.closing_code,
                    s.serial_number,
                    d.serial_number AS product_serial_number,
                    s.customer,
                    s.contact_name,
                    d.product_description,
                    d.city
                FROM wo_summary s
                LEFT JOIN wo_details d USING (work_order_id)
                WHERE TRIM(COALESCE(d.completion_date, '')) != ''
                  AND SUBSTR(d.completion_date, 1, 10) >= ?
            """, (cutoff,)).fetchall()
            all_closed_wo = [dict(r) for r in all_closed_wo]
            all_closed_wo_by_id: dict = {r["wo_id_str"]: r for r in all_closed_wo}
            for r in all_closed_wo:
                if r["case_number"]:
                    closed_by_case.setdefault(str(r["case_number"]), []).append(r)

            no_close_by_case: dict = {}
            _no_close_rows = conn.execute("""
                SELECT
                    CAST(s.work_order_id AS TEXT) AS wo_id_str,
                    d.completion_date,
                    d.closing_date,
                    s.work_order_type,
                    s.work_order_status,
                    d.case_number,
                    d.closing_code,
                    s.serial_number,
                    d.serial_number AS product_serial_number,
                    s.customer,
                    s.contact_name,
                    d.product_description,
                    d.city
                FROM wo_summary s
                LEFT JOIN wo_details d USING (work_order_id)
                WHERE d.case_number IS NOT NULL
                  AND TRIM(COALESCE(d.case_number, '')) != ''
                  AND TRIM(COALESCE(d.closing_date, '')) = ''
            """).fetchall()
            for r in _no_close_rows:
                r = dict(r)
                if r["case_number"]:
                    no_close_by_case.setdefault(str(r["case_number"]), []).append(r)

            def _disc_count_for(item_id):
                if not item_id:
                    return 0
                if item_id in disc_by_item:
                    return disc_by_item[item_id]
                row = edb.execute("""
                    SELECT COUNT(DISTINCT u.update_id) + COUNT(DISTINCT rep.reply_id) AS cnt
                    FROM item_updates u
                    LEFT JOIN item_update_replies rep ON rep.update_id = u.update_id
                    WHERE u.monday_item_id = ?
                """, (item_id,)).fetchone()
                cnt = row["cnt"] if row else 0
                disc_by_item[item_id] = cnt
                return cnt

            def _looks_like_wo(val):
                v = str(val or "").strip()
                return bool(v and _re.match(r'^40\d{8,}', v))

            _serials_needing_autofill = {
                str(dict(mr).get("serial_number") or "").strip().lower()
                for mr in all_monday
                if str(dict(mr).get("serial_number") or "").strip()
                and not str(dict(mr).get("wo_case_id") or "").strip()
            }
            _latest_wo_by_serial: dict = {}
            if _serials_needing_autofill:
                _sn_ph = ",".join("?" * len(_serials_needing_autofill))
                _latest_rows = conn.execute(f"""
                    SELECT LOWER(TRIM(s.serial_number)) AS sn_key,
                           CAST(s.work_order_id AS TEXT) AS wo_id
                    FROM wo_summary s
                    WHERE LOWER(TRIM(s.serial_number)) IN ({_sn_ph})
                    ORDER BY s.created_on DESC
                """, list(_serials_needing_autofill)).fetchall()
                for _lr in _latest_rows:
                    _lr = dict(_lr)
                    if _lr["sn_key"] not in _latest_wo_by_serial:
                        _latest_wo_by_serial[_lr["sn_key"]] = _lr["wo_id"]

            _unresolved_wo_ids = set()
            for _mr in all_monday:
                _rk = str(dict(_mr).get("wo_case_id") or "").strip()
                if _looks_like_wo(_rk) and _rk not in matched_wo_ids and _rk not in all_closed_wo_by_id:
                    _unresolved_wo_ids.add(_rk)

            _open_wo_by_id: dict = {}
            if _unresolved_wo_ids:
                _uid_ph = ",".join("?" * len(_unresolved_wo_ids))
                _open_rows = conn.execute(f"""
                    SELECT
                        CAST(s.work_order_id AS TEXT) AS wo_id_str,
                        s.work_order_type,
                        s.work_order_status,
                        s.serial_number,
                        d.serial_number      AS product_serial_number,
                        s.customer,
                        s.contact_name,
                        d.product_description,
                        d.city,
                        d.completion_date,
                        d.closing_date,
                        d.closing_code,
                        d.case_number
                    FROM wo_summary s
                    LEFT JOIN wo_details d USING (work_order_id)
                    WHERE CAST(s.work_order_id AS TEXT) IN ({_uid_ph})
                """, list(_unresolved_wo_ids)).fetchall()
                _open_wo_by_id = {dict(r)["wo_id_str"]: dict(r) for r in _open_rows}

            seen_monday_item_ids = set()

            for mrow in all_monday:
                mrow = dict(mrow)
                raw_key = str(mrow["wo_case_id"] or "").strip()

                item_id = mrow["monday_item_id"]
                if item_id in seen_monday_item_ids:
                    continue

                def _monday_only_row(key):
                    _sn_lo = str(mrow.get("serial_number") or "").strip().lower()
                    _auto_wo = _latest_wo_by_serial.get(_sn_lo) if _sn_lo else None
                    return {
                        "work_order_id":         None,
                        "serial_number":         mrow.get("serial_number") or None,
                        "product_serial_number": mrow.get("serial_number") or None,
                        "work_order_type":       mrow.get("work_order_type") or None,
                        "work_order_status":     None,
                        "customer":              None,
                        "contact_name":          None,
                        "product_description":   None,
                        "city":                  None,
                        "completion_date":       None,
                        "closing_date":          None,
                        "closing_code":          None,
                        "case_number":           None,
                        "esc_statuses":          mrow["status"],
                        "esc_created_at":        mrow["item_created_at"],
                        "esc_item_id":           item_id,
                        "esc_item_name":         mrow["item_name"],
                        "esc_board_id":          mrow["board_id"],
                        "esc_disc_count":        _disc_count_for(item_id),
                        "wo_case_id":            key,
                        "wo_case_match":         None,
                        "row_source":            "monday_only",
                        "latest_wo_id":          _auto_wo,
                    }

                if not raw_key:
                    seen_monday_item_ids.add(item_id)
                    monday_extra_rows.append(_monday_only_row(""))
                elif _looks_like_wo(raw_key):
                    if raw_key in matched_wo_ids:
                        continue
                    seen_monday_item_ids.add(item_id)
                    closed_wo = all_closed_wo_by_id.get(raw_key) or _open_wo_by_id.get(raw_key)
                    disc = _disc_count_for(item_id)
                    _sn_lo2 = str(mrow.get("serial_number") or "").strip().lower()
                    extra = {
                        "work_order_id":          closed_wo["wo_id_str"] if closed_wo else None,
                        "serial_number":          closed_wo["serial_number"] if closed_wo else (mrow.get("serial_number") or None),
                        "product_serial_number":  closed_wo["product_serial_number"] if closed_wo else (mrow.get("serial_number") or None),
                        "work_order_type":        closed_wo["work_order_type"] if closed_wo else (mrow.get("work_order_type") or None),
                        "work_order_status":      closed_wo["work_order_status"] if closed_wo else None,
                        "customer":               closed_wo["customer"] if closed_wo else None,
                        "contact_name":           closed_wo["contact_name"] if closed_wo else None,
                        "product_description":    closed_wo["product_description"] if closed_wo else None,
                        "city":                   closed_wo["city"] if closed_wo else None,
                        "completion_date":        closed_wo["completion_date"] if closed_wo else None,
                        "closing_date":           closed_wo["closing_date"] if closed_wo else None,
                        "closing_code":           closed_wo["closing_code"] if closed_wo else None,
                        "case_number":            closed_wo["case_number"] if closed_wo else None,
                        "esc_statuses":           mrow["status"],
                        "esc_created_at":         mrow["item_created_at"],
                        "esc_item_id":            item_id,
                        "esc_item_name":          mrow["item_name"],
                        "esc_board_id":           mrow["board_id"],
                        "esc_disc_count":         disc,
                        "wo_case_id":             raw_key,
                        "wo_case_match":          "wo" if closed_wo else None,
                        "row_source":             "monday_wo" if closed_wo else "monday_only",
                        "latest_wo_id":           None if closed_wo else _latest_wo_by_serial.get(_sn_lo2),
                    }
                    monday_extra_rows.append(extra)
                else:
                    if raw_key in matched_case_ids:
                        continue
                    esc_date_str = (mrow["item_created_at"] or "")[:10]
                    if not esc_date_str:
                        seen_monday_item_ids.add(item_id)
                        monday_extra_rows.append(_monday_only_row(raw_key))
                        continue
                    try:
                        _esc_dt = _dt.date.fromisoformat(esc_date_str)
                    except ValueError:
                        seen_monday_item_ids.add(item_id)
                        monday_extra_rows.append(_monday_only_row(raw_key))
                        continue

                    matched_wo = None
                    for cwo in closed_by_case.get(raw_key, []):
                        _ref_dates = [
                            (cwo.get("completion_date") or "")[:10],
                            (cwo.get("closing_date")    or "")[:10],
                        ]
                        for _rd in _ref_dates:
                            if not _rd:
                                continue
                            try:
                                _ref_dt = _dt.date.fromisoformat(_rd)
                            except ValueError:
                                continue
                            if -4 <= (_esc_dt - _ref_dt).days <= 7:
                                matched_wo = cwo
                                break
                        if matched_wo:
                            break

                    no_close_wo = None
                    if not matched_wo:
                        candidates_nc = no_close_by_case.get(raw_key, [])
                        if candidates_nc:
                            no_close_wo = candidates_nc[0]

                    if not matched_wo and not no_close_wo:
                        seen_monday_item_ids.add(item_id)
                        monday_extra_rows.append(_monday_only_row(raw_key))
                        continue

                    seen_monday_item_ids.add(item_id)
                    disc = _disc_count_for(item_id)
                    src_wo = matched_wo or no_close_wo
                    extra = {
                        "work_order_id":          src_wo["wo_id_str"],
                        "serial_number":          src_wo["serial_number"],
                        "product_serial_number":  src_wo["product_serial_number"],
                        "work_order_type":        src_wo["work_order_type"],
                        "work_order_status":      src_wo["work_order_status"],
                        "customer":               src_wo["customer"],
                        "contact_name":           src_wo["contact_name"],
                        "product_description":    src_wo["product_description"],
                        "city":                   src_wo["city"],
                        "completion_date":        src_wo["completion_date"],
                        "closing_date":           src_wo["closing_date"],
                        "closing_code":           src_wo["closing_code"],
                        "case_number":            src_wo["case_number"],
                        "esc_statuses":           mrow["status"],
                        "esc_created_at":         mrow["item_created_at"],
                        "esc_item_id":            item_id,
                        "esc_item_name":          mrow["item_name"],
                        "esc_board_id":          mrow["board_id"],
                        "esc_disc_count":         disc,
                        "wo_case_id":             raw_key,
                        "wo_case_match":          "case",
                        "row_source":             "monday_case",
                        "no_close_date":          no_close_wo is not None,
                    }
                    monday_extra_rows.append(extra)

        except Exception as _e:
            current_app.logger.error("api_dashboard_closing_codes error: %s", _e)
            for r in wo_rows:
                r["esc_statuses"]   = None
                r["esc_created_at"] = None
                r["esc_item_id"]    = None
                r["esc_item_name"]  = None
                r["esc_board_id"]   = None
                r["esc_disc_count"] = 0
                r["wo_case_id"]     = None
                r["wo_case_match"]  = None
        finally:
            edb.close()
    else:
        for r in wo_rows:
            r["esc_statuses"]   = None
            r["esc_created_at"] = None
            r["esc_item_id"]    = None
            r["esc_item_name"]  = None
            r["esc_board_id"]   = None
            r["esc_disc_count"] = 0
            r["wo_case_id"]     = None
            r["wo_case_match"]  = None

    combined = wo_rows + monday_extra_rows
    combined.sort(
        key=lambda r: (r.get("esc_created_at") or ""),
        reverse=True,
    )

    rows = []
    for r in combined:
        r.pop("wo_id_str",    None)
        r.pop("case_num_str", None)
        rows.append(r)

    return jsonify({"rows": [dict(r) for r in rows], "cutoff": cutoff})


@asp_bp.route("/asp/api/monday-discussion/<item_id>", methods=["GET"])
@login_required
def api_monday_discussion(item_id: str):
    """Return discussion updates and replies for a Monday item."""
    import os as _os
    from app.services.database.db import open_db
    project_root = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
    esc_db_path  = _os.path.join(project_root, "files", "lenovo_asp_escalation.db")
    result = {"updates": []}
    if not _os.path.isfile(esc_db_path):
        return jsonify(result)

    edb = open_db(esc_db_path)
    try:
        updates = edb.execute("""
            SELECT u.update_id, u.body_text, u.created_at, u.updated_at,
                   u.creator_id, c.creator_name
            FROM item_updates u
            LEFT JOIN creators c ON u.creator_id = c.creator_id
            WHERE u.monday_item_id = ?
            ORDER BY u.created_at ASC
        """, (item_id,)).fetchall()

        updates_out = []
        for upd in updates:
            upd_dict = dict(upd)
            replies = edb.execute("""
                SELECT r.reply_id, r.body_text, r.created_at,
                       r.creator_id, c.creator_name
                FROM item_update_replies r
                LEFT JOIN creators c ON r.creator_id = c.creator_id
                WHERE r.update_id = ?
                ORDER BY r.created_at ASC
            """, (upd_dict["update_id"],)).fetchall()
            upd_dict["replies"] = [dict(r) for r in replies]
            updates_out.append(upd_dict)

        result["updates"] = updates_out
    except Exception as _e:
        current_app.logger.error("api_monday_discussion error: %s", _e)
    finally:
        edb.close()

    return jsonify(result)


@asp_bp.route("/asp/api/escalation-inprogress", methods=["GET"])
@login_required
def api_escalation_inprogress():
    """Return all in-progress Monday escalations for this ASP's boards.

    In-progress = status is non-empty AND not in (complete, completed, reject, approved to order).
    Filtered to boards whose monday_board_id matches the current session's labor_vendor.
    """
    import os as _os, math as _math
    from app.services.database.db import get_db, open_db
    from flask import request as _req

    vf = _vendor_filter()

    try:
        page     = max(1, int(_req.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(200, max(1, int(_req.args.get("per_page", 50))))
    except (ValueError, TypeError):
        per_page = 50

    q       = (_req.args.get("q") or "").strip()
    wo_type = (_req.args.get("wo_type") or "").strip()

    project_root  = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
    esc_db_path   = _os.path.join(project_root, "files", "lenovo_asp_escalation.db")
    main_db_path  = _os.path.join(project_root, "files", "lenovo_asp.db")

    if not _os.path.isfile(esc_db_path):
        return jsonify({"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": per_page})

    COMPLETE_VALS = ["complete", "completed", "reject", "approved to order"]

    # Build WHERE
    where_parts = [
        "COALESCE(te.status,'') != ''",
        "LOWER(COALESCE(te.status,'')) NOT IN ({})".format(",".join("?" * len(COMPLETE_VALS))),
    ]
    params = list(COMPLETE_VALS)

    # Filter to this ASP's boards via asp_details
    if vf:
        where_parts.append("""
            te.board_id IN (
                SELECT CAST(ad.monday_board_id AS TEXT)
                FROM main_db.asp_details ad
                WHERE ad.labor_vendor_related = ?
                  AND ad.monday_board_id IS NOT NULL
            )
        """)
        params.append(vf)

    if wo_type:
        where_parts.append("UPPER(COALESCE(te.work_order_type,'')) = ?")
        params.append(wo_type.upper())

    if q:
        where_parts.append(
            "(te.item_name LIKE ? OR te.wo_case_id LIKE ? "
            "OR te.serial_number LIKE ? OR te.status LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    where_sql = "WHERE " + " AND ".join(where_parts)

    edb = open_db(esc_db_path)
    rows_list = []
    total = 0
    try:
        edb.execute(f"ATTACH DATABASE '{main_db_path}' AS main_db")
    except Exception:
        pass
    try:
        total = edb.execute(
            f"SELECT COUNT(*) FROM technical_escalation te {where_sql}", params
        ).fetchone()[0]

        offset = (page - 1) * per_page
        raw = edb.execute(f"""
            SELECT
                te.monday_item_id,
                te.board_id,
                te.asp_board,
                te.item_name,
                te.item_created_at,
                te.status,
                te.work_order_type,
                te.wo_case_id,
                te.serial_number,
                (
                    SELECT COUNT(DISTINCT u2.update_id) + COUNT(DISTINCT r2.reply_id)
                    FROM item_updates u2
                    LEFT JOIN item_update_replies r2 ON u2.update_id = r2.update_id
                    WHERE u2.monday_item_id = te.monday_item_id
                ) AS disc_count,
                CASE
                    WHEN te.serial_number IS NULL OR te.serial_number = '' THEN 0
                    WHEN EXISTS (
                        SELECT 1 FROM main_db.wo_summary ws
                        WHERE LOWER(ws.serial_number) = LOWER(te.serial_number)
                    ) THEN 1
                    ELSE 0
                END AS has_wo
            FROM technical_escalation te
            {where_sql}
            ORDER BY te.item_created_at DESC
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()
        rows_list = [dict(r) for r in raw]
    except Exception as _e:
        current_app.logger.error("api_escalation_inprogress query failed: %s", _e)
    finally:
        edb.close()

    pages = max(1, _math.ceil(total / per_page)) if total else 1
    return jsonify({
        "rows":     rows_list,
        "total":    total,
        "page":     page,
        "pages":    pages,
        "per_page": per_page,
    })




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


@asp_bp.route("/asp/api/operation-support", methods=["POST"])
@login_required
def api_save_operation_support():
    """Save updated operation_support value for the logged-in ASP user."""
    from app.services.database.db import get_db
    uid  = session.get("user_id")
    role = session.get("role", "")
    if role != "asp":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    value = (data.get("operation_support") or "").strip()
    allowed = {"CCI Only", "CCI & ONSITE"}
    if value not in allowed:
        return jsonify({"error": "Invalid value. Must be 'CCI Only' or 'CCI & ONSITE'."}), 400
    get_db().execute(
        "UPDATE asp_details SET operation_support = ? WHERE id = ?",
        (value, uid)
    )
    get_db().commit()
    return jsonify({"ok": True, "operation_support": value})


@asp_bp.route("/asp/api/request-password-change", methods=["POST"])
@login_required
def api_request_password_change():
    """Submit and auto-approve a password change for the logged-in ASP account."""
    from app.services.database.db import get_db
    uid      = session.get("user_id")
    username = session.get("username")
    role     = session.get("role", "")
    if role != "asp":
        return jsonify({"error": "Forbidden"}), 403
    data         = request.get_json(silent=True) or {}
    new_password = (data.get("new_password") or "").strip()
    if not new_password or len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    db = get_db()
    # Cancel any existing pending request first
    db.execute(
        "UPDATE asp_pw_change_requests SET status='cancelled' "
        "WHERE asp_username=? AND status='pending'",
        (username,)
    )
    # Record the request and immediately mark it as approved (auto)
    db.execute(
        "INSERT INTO asp_pw_change_requests "
        "(asp_username, new_password, status, reviewed_by, reviewed_at) "
        "VALUES (?, ?, 'approved', 'auto', datetime('now'))",
        (username, new_password)
    )
    # Apply the new password directly
    db.execute(
        "UPDATE asp_details SET password=? WHERE username=?",
        (new_password, username)
    )
    db.commit()
    return jsonify({"ok": True, "message": "Password changed successfully."})


@asp_bp.route("/asp/api/location", methods=["PATCH"])
@login_required
def api_save_location():
    """Save updated location fields for the logged-in ASP account."""
    from app.services.database.db import get_db
    uid  = session.get("user_id")
    role = session.get("role", "")
    if role != "asp":
        return jsonify({"error": "Forbidden"}), 403
    data       = request.get_json(silent=True) or {}
    store_name = (data.get("store_name")    or "").strip() or None
    kota       = (data.get("kota")          or "").strip() or None
    island     = (data.get("island")        or "").strip() or None
    phone      = (data.get("phone_number")  or "").strip() or None
    address    = (data.get("address")       or "").strip() or None
    db = get_db()
    db.execute(
        "UPDATE asp_details SET store_name=?, kota=?, island=?, "
        "phone_number=?, address=?, updated_at=datetime('now') WHERE id=?",
        (store_name, kota, island, phone, address, uid)
    )
    db.commit()
    row = db.execute(
        "SELECT store_name, kota, island, phone_number, address "
        "FROM asp_details WHERE id=?", (uid,)
    ).fetchone()
    return jsonify({"ok": True, "location": dict(row)})


# ── ASP Users API ─────────────────────────────────────────────────────────────

def _asp_users_forbidden():
    """Only the parent ASP account may manage (create/edit) asp_users."""
    if session.get("role") != "asp":
        return jsonify({"error": "Forbidden"}), 403
    return None


@asp_bp.route("/asp/api/users", methods=["GET"])
@login_required
def api_list_asp_users():
    """Return all users belonging to the logged-in ASP."""
    err = _asp_users_forbidden()
    if err: return err
    from app.services.database.db import get_db
    labor_vendor = session.get("labor_vendor")
    rows = get_db().execute(
        "SELECT id, tech_id, full_name, email, password, phone_number, is_active, created_at "
        "FROM asp_users WHERE labor_vendor_related = ? ORDER BY id",
        (labor_vendor,)
    ).fetchall()
    return jsonify({"ok": True, "users": [dict(r) for r in rows]})


@asp_bp.route("/asp/api/users", methods=["POST"])
@login_required
def api_create_asp_user():
    """Create a new user under the logged-in ASP."""
    err = _asp_users_forbidden()
    if err: return err
    from app.services.database.db import get_db
    labor_vendor = session.get("labor_vendor")
    data      = request.get_json(silent=True) or {}
    full_name = (data.get("full_name")    or "").strip()
    email     = (data.get("email")        or "").strip()
    password  = (data.get("password")     or "").strip()
    phone     = (data.get("phone_number") or "").strip() or None
    tech_id   = (data.get("tech_id")      or "").strip() or None
    if not full_name:
        return jsonify({"error": "full_name is required"}), 400
    if not email:
        return jsonify({"error": "email is required"}), 400
    if not password or len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    db = get_db()
    # Uniqueness checks across the entire asp_users table
    dup_email = db.execute(
        "SELECT id FROM asp_users WHERE LOWER(email) = LOWER(?)",
        (email,)
    ).fetchone()
    if dup_email:
        return jsonify({"error": "That email address is already registered.", "field": "email"}), 409
    dup_name = db.execute(
        "SELECT id FROM asp_users WHERE LOWER(full_name) = LOWER(?)",
        (full_name,)
    ).fetchone()
    if dup_name:
        return jsonify({"error": "That full name is already registered.", "field": "full_name"}), 409
    cur = db.execute(
        "INSERT INTO asp_users (labor_vendor_related, tech_id, full_name, email, password, phone_number) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (labor_vendor, tech_id, full_name, email, password, phone)
    )
    db.commit()
    new_id = cur.lastrowid
    row = db.execute(
        "SELECT id, tech_id, full_name, email, password, phone_number, is_active, created_at "
        "FROM asp_users WHERE id = ?", (new_id,)
    ).fetchone()
    return jsonify({"ok": True, "user": dict(row)}), 201


@asp_bp.route("/asp/api/users/<int:user_id>", methods=["PUT"])
@login_required
def api_update_asp_user(user_id):
    """Update an existing ASP user (must belong to the logged-in ASP)."""
    err = _asp_users_forbidden()
    if err: return err
    from app.services.database.db import get_db
    labor_vendor = session.get("labor_vendor")
    db   = get_db()
    # Verify ownership
    existing = db.execute(
        "SELECT id FROM asp_users WHERE id = ? AND labor_vendor_related = ?",
        (user_id, labor_vendor)
    ).fetchone()
    if not existing:
        return jsonify({"error": "User not found"}), 404
    data      = request.get_json(silent=True) or {}
    # Fetch current values so partial updates (e.g. contact-only) don't wipe other fields
    current = db.execute(
        "SELECT full_name, email, password, phone_number FROM asp_users WHERE id = ?",
        (user_id,)
    ).fetchone()
    full_name = (data.get("full_name") or "").strip() or (current["full_name"] or "")
    email     = (data.get("email")     or "").strip() or (current["email"]     or "")
    password  = (data.get("password")  or "").strip()
    # phone_number key present → use it (even if empty string → None); key absent → keep current
    if "phone_number" in data:
        phone = (data.get("phone_number") or "").strip() or None
    else:
        phone = current["phone_number"]
    if not full_name:
        return jsonify({"error": "full_name is required"}), 400
    if not email:
        return jsonify({"error": "email is required"}), 400
    # Uniqueness checks across the entire asp_users table, excluding the current user
    dup_email = db.execute(
        "SELECT id FROM asp_users WHERE LOWER(email) = LOWER(?) AND id != ?",
        (email, user_id)
    ).fetchone()
    if dup_email:
        return jsonify({"error": "That email address is already registered.", "field": "email"}), 409
    dup_name = db.execute(
        "SELECT id FROM asp_users WHERE LOWER(full_name) = LOWER(?) AND id != ?",
        (full_name, user_id)
    ).fetchone()
    if dup_name:
        return jsonify({"error": "That full name is already registered.", "field": "full_name"}), 409
    # Only update password when a new one is supplied
    if password:
        if len(password) < 8:
            return jsonify({"error": "password must be at least 8 characters"}), 400
        db.execute(
            "UPDATE asp_users SET full_name=?, email=?, password=?, "
            "phone_number=?, updated_at=datetime('now') WHERE id=?",
            (full_name, email, password, phone, user_id)
        )
    else:
        db.execute(
            "UPDATE asp_users SET full_name=?, email=?, "
            "phone_number=?, updated_at=datetime('now') WHERE id=?",
            (full_name, email, phone, user_id)
        )
    db.commit()
    row = db.execute(
        "SELECT id, tech_id, full_name, email, password, phone_number, is_active, created_at "
        "FROM asp_users WHERE id = ?", (user_id,)
    ).fetchone()
    return jsonify({"ok": True, "user": dict(row)})


@asp_bp.route("/asp/api/users/<int:user_id>", methods=["DELETE"])
@login_required
def api_delete_asp_user(user_id):
    """Permanently delete an ASP user (must belong to the logged-in ASP)."""
    err = _asp_users_forbidden()
    if err: return err
    from app.services.database.db import get_db
    labor_vendor = session.get("labor_vendor")
    db = get_db()
    existing = db.execute(
        "SELECT id FROM asp_users WHERE id = ? AND labor_vendor_related = ?",
        (user_id, labor_vendor)
    ).fetchone()
    if not existing:
        return jsonify({"error": "User not found"}), 404
    db.execute("DELETE FROM asp_users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"ok": True})


@asp_bp.route("/asp/api/users/<int:user_id>/status", methods=["PATCH"])
@login_required
def api_toggle_asp_user_status(user_id):
    """Toggle is_active for an ASP user (must belong to the logged-in ASP)."""
    err = _asp_users_forbidden()
    if err: return err
    from app.services.database.db import get_db
    labor_vendor = session.get("labor_vendor")
    db = get_db()
    existing = db.execute(
        "SELECT id, is_active FROM asp_users WHERE id = ? AND labor_vendor_related = ?",
        (user_id, labor_vendor)
    ).fetchone()
    if not existing:
        return jsonify({"error": "User not found"}), 404
    data       = request.get_json(silent=True) or {}
    new_status = 1 if data.get("is_active") else 0
    db.execute(
        "UPDATE asp_users SET is_active = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, user_id)
    )
    db.commit()
    row = db.execute(
        "SELECT id, tech_id, full_name, email, password, phone_number, is_active, created_at "
        "FROM asp_users WHERE id = ?", (user_id,)
    ).fetchone()
    return jsonify({"ok": True, "user": dict(row)})


@asp_bp.route("/asp/api/completed-last-30days", methods=["GET"])
@login_required
def api_completed_last_30days():
    """Completed Last 30 Days — WOs whose completion_date is within the past 30 days."""
    from app.services.database.queries import get_asp_completed_last_30_days
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_completed_last_30_days(
        search         = request.args.get("q", "").strip(),
        type_filter    = request.args.get("wo_type", "").strip(),
        no_awb         = _bool_arg("no_awb", False),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
    ))


@asp_bp.route("/asp/api/in-prepare", methods=["GET"])
@login_required
def api_in_prepare():
    """In-Prepare Follow-Up — WOs with part ordered but not yet shipped."""
    from app.services.database.queries import get_asp_in_prepare_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_in_prepare_page(
        search          = request.args.get("q", "").strip(),
        page            = _int_arg("page", 1),
        page_size       = per_page,
        vendor_filter   = _vendor_filter(),
        tech_id_filter  = _tech_id_filter(),
        prepare_filter  = request.args.get("prepare_filter", "").strip(),
        wo_type_filter  = request.args.get("wo_type", "").strip(),
    ))



@asp_bp.route("/asp/api/return-part", methods=["GET"])
@login_required
def api_return_part():
    """Return Part Follow-Up — WOs with is_exist_excel='yes' SOIDs; computed need_to_return / weekly_dc_report state."""
    from app.services.database.queries import get_asp_part_return_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_asp_part_return_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
    ))


@asp_bp.route("/asp/api/return-reminder", methods=["GET"])
@login_required
def api_return_reminder():
    """Return Reminder — closed-WO SOIDs with PENDING WITH PARTNER or PENDING FOR DC GENERATION return_status."""
    from app.services.database.queries import get_return_reminder_page
    per_page = min(_int_arg("per_page", 25), 100)
    return jsonify(get_return_reminder_page(
        search              = request.args.get("q", "").strip(),
        return_status       = request.args.get("return_status", "").strip(),
        return_flag_filter  = request.args.get("return_flag_filter", "").strip(),
        page                = _int_arg("page", 1),
        page_size           = per_page,
        vendor_filter       = _vendor_filter(),
        tech_id_filter      = _tech_id_filter(),
    ))


@asp_bp.route("/asp/api/return-reminder/export", methods=["GET"])
@login_required
def api_return_reminder_export():
    """Export Return Reminder rows (one row per SOID) for the active sub-tab filter."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from app.services.database.queries import get_return_reminder_page

    rs   = request.args.get("return_status",      "").strip()
    q    = request.args.get("q",                  "").strip()
    flag = request.args.get("return_flag_filter", "").strip()

    result = get_return_reminder_page(
        search             = q,
        return_status      = rs,
        return_flag_filter = flag,
        page               = 1,
        page_size          = 9999,
        vendor_filter      = _vendor_filter(),
        tech_id_filter     = _tech_id_filter(),
    )
    rows = result.get("rows", [])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Return Reminder"

    headers = ["No.", "SOID", "WO Number", "Created On", "WO Type",
               "Return Status", "WO Complete Date", "WO Status", "Contact Name", "ASP"]
    col_keys = [None, "soid", "work_order_id", "created_on", "work_order_type",
                "return_status", "completion_date", "work_order_status", "contact_name", "customer"]
    col_widths = [6, 18, 16, 18, 14, 26, 20, 24, 24, 28]

    hdr_fill = PatternFill("solid", fgColor="1F2328")
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[cell.column_letter].width = w

    for ri, r in enumerate(rows, 2):
        ws.cell(row=ri, column=1, value=ri - 1)
        for ci, key in enumerate(col_keys[1:], 2):
            val = r.get(key, "")
            if val is None:
                val = ""
            ws.cell(row=ri, column=ci, value=str(val) if val != "" else "")

    import io
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_rs = (rs or "all").replace(" ", "_").lower()
    from flask import send_file
    return send_file(buf, as_attachment=True,
                     download_name=f"return_reminder_{safe_rs}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@asp_bp.route("/asp/api/return-part/export", methods=["GET"])
@login_required
def api_return_part_export():
    """Export Return Part Follow-Up rows expanded by SOID (one row per part line)
    for every WO that exists on the Return Part tab (followup_state=need_to_return).
    Saves to files/report/ and streams the file back as a download."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from app.services.database.queries import get_asp_part_return_page
    from app.services.database.db import get_db

    # ── 1. Collect WOs in the Return Part tab ─────────────────────────────────
    result = get_asp_part_return_page(
        search         = request.args.get("q", "").strip(),
        followup_state = "need_to_return",
        page           = 1,
        page_size      = 9999,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
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
                p.return_status,
                p.awb,
                p.ship_pou_pod_time,
                p.dc_number
            FROM wo_product_detail p
            WHERE p.work_order_id IN ({placeholders})
              AND UPPER(TRIM(COALESCE(p.return_status,''))) IN (
                  'PENDING WITH PARTNER','PENDING FOR DC GENERATION'
              )
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
            "return_status":                p.get("return_status", ""),
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
        "Part Status", "Return Status", "AWB", "POD Date", "DC Number",
    ]
    col_keys = [
        None,
        "work_order_id", "created_on", "work_order_type", "case_desc",
        "work_order_status", "committed_delivery_date", "actual_committed_onsite_date",
        "contact_name", "customer",
        "soid", "product", "description", "order_date", "delivery_date",
        "wo_product_status", "return_status", "awb", "ship_pou_pod_time", "dc_number",
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


# ═══════════════════════════════════════════════════════════════════════════
#  In-Prepare Follow-Up — Export
# ═══════════════════════════════════════════════════════════════════════════

@asp_bp.route("/asp/api/in-prepare/export", methods=["GET"])
@login_required
def api_in_prepare_export():
    """Export In-Prepare Follow-Up — all rows, one per WO, as .xlsx."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from app.services.database.queries import get_asp_in_prepare_page

    result = get_asp_in_prepare_page(
        search          = request.args.get("q", "").strip(),
        prepare_filter  = request.args.get("prepare_filter", "").strip(),
        page            = 1,
        page_size       = 9999,
        vendor_filter   = _vendor_filter(),
        tech_id_filter  = _tech_id_filter(),
    )
    wo_rows = result.get("rows", [])

    def _fd(val):
        if not val: return ""
        s = str(val).strip()
        return s[:16] if len(s) > 16 else s

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "In-Prepare Follow-Up"

    headers = [
        "No.", "WO Number", "Created On", "WO Type", "Case",
        "WO Status", "Part Product", "Part Description",
        "Part Order Date", "ETA tiba di YCH", "Part SOID",
        "On Hold", "Total Waiting Pickup",
        "Contact Name", "ASP",
    ]
    col_keys = [
        None,
        "work_order_id", "created_on", "work_order_type", "case_desc",
        "work_order_status", "part_product", "part_description",
        "part_order_date", "part_eta_wh", "part_soid",
        "part_on_hold_count", "part_waiting_pickup_count",
        "contact_name", "customer",
    ]
    col_widths = [6, 16, 18, 14, 34, 26, 20, 34, 18, 18, 14, 12, 18, 24, 32]

    hdr_fill   = PatternFill("solid", fgColor="1F2328")
    hdr_font   = Font(bold=True, color="FFFFFF", size=11)
    hdr_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side  = Side(style="thin", color="E5E7EB")
    thin_bdr   = Border(left=thin_side, right=thin_side, bottom=thin_side, top=thin_side)

    for ci, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = hdr_align; cell.border = thin_bdr
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 22

    even_fill  = PatternFill("solid", fgColor="F7F8FA")
    data_font  = Font(size=11)
    data_align = Alignment(vertical="center")

    for ri, r in enumerate(wo_rows, start=2):
        fill = even_fill if ri % 2 == 0 else PatternFill()
        for ci, key in enumerate(col_keys, start=1):
            if key is None:
                value = ri - 1
            elif key in ("created_on", "part_order_date", "part_eta_wh"):
                value = _fd(r.get(key))
            elif key == "part_soid":
                raw = r.get(key)
                value = str(int(raw)) if raw is not None else ""
            else:
                value = r.get(key) or ""
            cell = ws.cell(row=ri, column=ci, value=value)
            cell.font = data_font; cell.alignment = data_align; cell.border = thin_bdr
            if fill.fill_type: cell.fill = fill
        ws.row_dimensions[ri].height = 18

    ws.freeze_panes = "A2"

    report_dir = current_app.config["REPORT_DIR"]
    os.makedirs(report_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"InPrepare_FollowUp_{ts}.xlsx"
    filepath = os.path.join(report_dir, filename)
    wb.save(filepath)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ═══════════════════════════════════════════════════════════════════════════
#  CCI Follow-Up — Export
# ═══════════════════════════════════════════════════════════════════════════

@asp_bp.route("/asp/api/cci-followup/export", methods=["GET"])
@login_required
def api_cci_followup_export():
    """Export CCI Follow-Up — all rows as .xlsx."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from app.services.database.queries import get_asp_cci_followup_page

    result = get_asp_cci_followup_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = 1,
        page_size      = 9999,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
    )
    wo_rows = result.get("rows", [])

    def _fd(val):
        if not val: return ""
        s = str(val).strip()
        return s[:16] if len(s) > 16 else s

    _state_labels = {
        "confirm_receipt": "Confirm AWB",
        "part_sla":        "Part SLA Overdue",
        "wo_sla":          "Escalate WO",
        "report_problem":  "WO SLA Follow-Up",
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CCI Follow-Up"

    headers = [
        "No.", "WO Number", "Created On", "Follow-Up State",
        "Part Qty (In Transit)", "Target Fix/Sampai",
        "AWB", "WO Status", "Shipped On", "Case",
        "Contact Name", "ASP",
    ]
    col_keys = [
        None,
        "work_order_id", "created_on", "_followup_label",
        "part_qty", "part_eta",
        "part_awb", "work_order_status", "ship_pickup_time", "case_desc",
        "contact_name", "customer",
    ]
    col_widths = [6, 16, 18, 22, 18, 22, 20, 26, 20, 34, 24, 32]

    hdr_fill   = PatternFill("solid", fgColor="1F2328")
    hdr_font   = Font(bold=True, color="FFFFFF", size=11)
    hdr_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side  = Side(style="thin", color="E5E7EB")
    thin_bdr   = Border(left=thin_side, right=thin_side, bottom=thin_side, top=thin_side)

    for ci, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = hdr_align; cell.border = thin_bdr
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 22

    even_fill  = PatternFill("solid", fgColor="F7F8FA")
    data_font  = Font(size=11)
    data_align = Alignment(vertical="center")

    for ri, r in enumerate(wo_rows, start=2):
        fill = even_fill if ri % 2 == 0 else PatternFill()
        for ci, key in enumerate(col_keys, start=1):
            if key is None:
                value = ri - 1
            elif key == "_followup_label":
                value = _state_labels.get(r.get("followup_state", ""), r.get("followup_state", "") or "")
            elif key in ("created_on", "part_eta", "ship_pickup_time"):
                value = _fd(r.get(key))
            else:
                value = r.get(key) or ""
            cell = ws.cell(row=ri, column=ci, value=value)
            cell.font = data_font; cell.alignment = data_align; cell.border = thin_bdr
            if fill.fill_type: cell.fill = fill
        ws.row_dimensions[ri].height = 18

    ws.freeze_panes = "A2"

    report_dir = current_app.config["REPORT_DIR"]
    os.makedirs(report_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"CCI_FollowUp_{ts}.xlsx"
    filepath = os.path.join(report_dir, filename)
    wb.save(filepath)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ═══════════════════════════════════════════════════════════════════════════
#  ONS (Onsite) Follow-Up — Export
# ═══════════════════════════════════════════════════════════════════════════

@asp_bp.route("/asp/api/onsite-followup/export", methods=["GET"])
@login_required
def api_onsite_followup_export():
    """Export ONS Follow-Up — all rows as .xlsx."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from app.services.database.queries import get_asp_onsite_followup_page

    result = get_asp_onsite_followup_page(
        search         = request.args.get("q", "").strip(),
        followup_state = request.args.get("followup_state", "").strip(),
        page           = 1,
        page_size      = 9999,
        vendor_filter  = _vendor_filter(),
        tech_id_filter = _tech_id_filter(),
    )
    wo_rows = result.get("rows", [])

    def _fd(val):
        if not val: return ""
        s = str(val).strip()
        return s[:16] if len(s) > 16 else s

    _state_labels = {
        "wo_reschedule":  "ONS In-Transit",
        "part_sla":       "Part SLA Overdue",
        "wo_sla":         "Escalate WO",
        "report_problem": "WO SLA Follow-Up",
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ONS Follow-Up"

    headers = [
        "No.", "WO Number", "Created On", "Follow-Up State",
        "Delivered On", "Part Qty (In Transit)", "Defer Date",
        "Target Fix/Sampai",
        "AWB", "WO Status", "Shipped On", "POD / Received On",
        "Case", "Contact Name", "ASP",
    ]
    col_keys = [
        None,
        "work_order_id", "created_on", "_followup_label",
        "part_pod_time", "part_qty", "customer_defer_date",
        "part_eta",
        "part_awb", "work_order_status", "ship_pickup_time", "part_pod_time",
        "case_desc", "contact_name", "customer",
    ]
    col_widths = [6, 16, 18, 20, 20, 18, 18, 22, 20, 26, 20, 20, 34, 24, 32]

    hdr_fill   = PatternFill("solid", fgColor="1F2328")
    hdr_font   = Font(bold=True, color="FFFFFF", size=11)
    hdr_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side  = Side(style="thin", color="E5E7EB")
    thin_bdr   = Border(left=thin_side, right=thin_side, bottom=thin_side, top=thin_side)

    for ci, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = hdr_align; cell.border = thin_bdr
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 22

    even_fill  = PatternFill("solid", fgColor="F7F8FA")
    data_font  = Font(size=11)
    data_align = Alignment(vertical="center")

    for ri, r in enumerate(wo_rows, start=2):
        fill = even_fill if ri % 2 == 0 else PatternFill()
        for ci, key in enumerate(col_keys, start=1):
            if key is None:
                value = ri - 1
            elif key == "_followup_label":
                value = _state_labels.get(r.get("followup_state", ""), r.get("followup_state", "") or "")
            elif key in ("created_on", "part_eta", "ship_pickup_time",
                         "part_pod_time", "customer_defer_date"):
                value = _fd(r.get(key))
            else:
                value = r.get(key) or ""
            cell = ws.cell(row=ri, column=ci, value=value)
            cell.font = data_font; cell.alignment = data_align; cell.border = thin_bdr
            if fill.fill_type: cell.fill = fill
        ws.row_dimensions[ri].height = 18

    ws.freeze_panes = "A2"

    report_dir = current_app.config["REPORT_DIR"]
    os.makedirs(report_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"ONS_FollowUp_{ts}.xlsx"
    filepath = os.path.join(report_dir, filename)
    wb.save(filepath)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Under Escalation — Export
# ═══════════════════════════════════════════════════════════════════════════

@asp_bp.route("/asp/api/under-escalation/export", methods=["GET"])
@login_required
def api_under_escalation_export():
    """Export Under Escalation table — respects wo_type, status, and search filters."""
    import openpyxl
    import json as _json
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    # Re-use the existing data function (same module, same request context).
    raw_resp = api_dashboard_closing_codes()
    all_rows = _json.loads(raw_resp.get_data(as_text=True)).get("rows", [])

    # ── Apply the same 3-stage filter the frontend uses ─────────────────────
    COMPLETE_VALS = {"complete", "completed", "reject", "approved to order"}

    type_val   = request.args.get("wo_type",  "").strip()
    status_val = request.args.get("status",   "").strip()   # in-progress | complete | no-status
    search_val = request.args.get("q",        "").strip().lower()

    def _passes(r):
        # 1. WO Type
        if type_val and (r.get("work_order_type") or "") != type_val:
            return False
        # 2. Monday Status
        if status_val:
            statuses = [s.strip().lower() for s in (r.get("esc_statuses") or "").split(",") if s.strip()]
            if status_val == "in-progress":
                if not any(s and s not in COMPLETE_VALS for s in statuses):
                    return False
            elif status_val == "complete":
                if not any(s in COMPLETE_VALS for s in statuses):
                    return False
            elif status_val == "no-status":
                if statuses:
                    return False
        # 3. Search
        if search_val:
            haystack = " ".join(
                str(r.get(k) or "").lower()
                for k in ("work_order_id", "work_order_type", "closing_code",
                          "work_order_status", "esc_statuses", "wo_case_id")
            )
            if search_val not in haystack:
                return False
        return True

    sort_by = request.args.get("sort_by", "").strip()
    rows = [r for r in all_rows if _passes(r)]
    if sort_by == "completion_date":
        rows.sort(
            key=lambda r: (r.get("completion_date") or r.get("closing_date") or ""),
            reverse=True,
        )

    # ── Build workbook ────────────────────────────────────────────────────────
    def _fd(val):
        if not val: return ""
        s = str(val).strip()
        return s[:16] if len(s) > 16 else s

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Under Escalation"

    headers = [
        "No.", "WO ID", "Completed", "Type",
        "Closing Code / WO Status",
        "WO/Case ID Monday", "Escalation Date", "Monday Status",
    ]
    col_keys = [
        None,
        "work_order_id", "completion_date", "work_order_type",
        "closing_code",
        "wo_case_id", "esc_created_at", "esc_statuses",
    ]
    col_widths = [6, 16, 18, 14, 34, 22, 20, 30]

    hdr_fill  = PatternFill("solid", fgColor="1F2328")
    hdr_font  = Font(bold=True, color="FFFFFF", size=11)
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side = Side(style="thin", color="E5E7EB")
    thin_bdr  = Border(left=thin_side, right=thin_side, bottom=thin_side, top=thin_side)

    for ci, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = hdr_align; cell.border = thin_bdr
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 22

    even_fill  = PatternFill("solid", fgColor="F7F8FA")
    data_font  = Font(size=11)
    data_align = Alignment(vertical="center")

    for ri, r in enumerate(rows, start=2):
        fill = even_fill if ri % 2 == 0 else PatternFill()
        for ci, key in enumerate(col_keys, start=1):
            if key is None:
                value = ri - 1
            elif key in ("completion_date", "esc_created_at"):
                value = _fd(r.get(key))
            elif key == "work_order_type":
                raw = r.get(key) or ""
                value = "Carry-In" if raw == "CCI" else ("Onsite" if raw == "ONS" else raw)
            else:
                value = r.get(key) or ""
            cell = ws.cell(row=ri, column=ci, value=value)
            cell.font = data_font; cell.alignment = data_align; cell.border = thin_bdr
            if fill.fill_type: cell.fill = fill
        ws.row_dimensions[ri].height = 18

    ws.freeze_panes = "A2"

    report_dir = current_app.config["REPORT_DIR"]
    os.makedirs(report_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Under_Escalation_{ts}.xlsx"
    filepath = os.path.join(report_dir, filename)
    wb.save(filepath)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
