"""
api_mobile.py — REST JSON endpoints for the Lenovo ASP mobile app.

All routes are prefixed /api/v1/.
Authentication uses JWT Bearer tokens (see jwt_service.py).
The session-cookie auth used by the web portal is NOT involved here.

Endpoints
---------
  POST /api/v1/auth/login          — exchange credentials for a JWT
  GET  /api/v1/mobile/stats        — WO summary stat counts
  GET  /api/v1/mobile/in-prepare   — In-Prepare Follow-Up list
  GET  /api/v1/mobile/cci-followup — CCI Follow-Up list
  GET  /api/v1/mobile/onsite-followup — Onsite Follow-Up list
  GET  /api/v1/mobile/return-part  — Return Part list
  GET  /api/v1/mobile/wo/<id>      — Single WO detail + parts
"""

from flask import Blueprint, request, jsonify, g
from app.services.jwt_service import generate_token, jwt_required, mobile_vendor_filter

mobile_bp = Blueprint("mobile", __name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _int_arg(name: str, default: int) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except (ValueError, TypeError):
        return default


def _str_arg(name: str) -> str:
    return (request.args.get(name) or "").strip()


# ── Auth ──────────────────────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/auth/login", methods=["POST"])
def mobile_login():
    """
    Accept JSON { username, password } and return a JWT on success.

    Supports all three credential stores in priority order:
      1. asp_users  (email-based login — the primary mobile user type)
      2. asp_details (ASP HQ account login)
      3. admin_users (rejected for mobile — admins don't use the mobile app)

    Returns 200 { token, role, display_name, username, labor_vendor, asp_name }
    Returns 401 on bad credentials or inactive account.
    Returns 403 if the account is an admin role (not allowed on mobile).
    """
    from app.services.database.db import get_db

    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    conn = get_db()

    # ── 1. Try asp_users first (email login) ─────────────────────────────────
    asp_user_row = conn.execute(
        "SELECT u.id, u.asp_username, u.full_name, u.email, u.password, "
        "       u.is_active, d.labor_vendor_related, d.service_provider "
        "FROM asp_users u "
        "JOIN asp_details d ON d.username = u.asp_username "
        "WHERE LOWER(u.email) = LOWER(?)",
        (username,),
    ).fetchone()

    if asp_user_row and str(asp_user_row["password"]) == password:
        if not asp_user_row["is_active"]:
            return jsonify({"error": "Your account is disabled. Contact your ASP administrator."}), 401
        token = generate_token(
            user_id      = asp_user_row["id"],
            username     = asp_user_row["asp_username"],
            role         = "asp_user",
            labor_vendor = asp_user_row["labor_vendor_related"],
            display_name = asp_user_row["full_name"] or asp_user_row["email"],
        )
        return jsonify({
            "token":        token,
            "role":         "asp_user",
            "display_name": asp_user_row["full_name"] or asp_user_row["email"],
            "username":     asp_user_row["asp_username"],
            "email":        asp_user_row["email"],
            "labor_vendor": asp_user_row["labor_vendor_related"],
            "asp_name":     asp_user_row["service_provider"] or asp_user_row["asp_username"],
        })

    # ── 2. Try asp_details (ASP HQ username login) ───────────────────────────
    asp_row = conn.execute(
        "SELECT id, username, password, service_provider, labor_vendor_related "
        "FROM asp_details WHERE LOWER(username) = LOWER(?)",
        (username,),
    ).fetchone()

    if asp_row and str(asp_row["password"]) == password:
        token = generate_token(
            user_id      = asp_row["id"],
            username     = asp_row["username"],
            role         = "asp",
            labor_vendor = asp_row["labor_vendor_related"],
            display_name = asp_row["service_provider"] or asp_row["username"],
        )
        return jsonify({
            "token":        token,
            "role":         "asp",
            "display_name": asp_row["service_provider"] or asp_row["username"],
            "username":     asp_row["username"],
            "email":        None,
            "labor_vendor": asp_row["labor_vendor_related"],
            "asp_name":     asp_row["service_provider"] or asp_row["username"],
        })

    # ── 3. Try admin_users — allow superadmin, reject others ─────────────────
    admin_row = conn.execute(
        "SELECT id, username, password, full_name, role, is_active "
        "FROM admin_users WHERE LOWER(username) = LOWER(?)",
        (username,),
    ).fetchone()

    if admin_row and str(admin_row["password"]) == password:
        if admin_row["role"] != "superadmin":
            return jsonify({"error": "Only superadmin accounts can log in to the mobile app."}), 403
        if not admin_row["is_active"]:
            return jsonify({"error": "Your account is disabled."}), 401
        token = generate_token(
            user_id      = admin_row["id"],
            username     = admin_row["username"],
            role         = "superadmin",
            labor_vendor = None,        # no vendor filter — sees all WOs
            display_name = admin_row["full_name"] or admin_row["username"],
        )
        return jsonify({
            "token":        token,
            "role":         "superadmin",
            "display_name": admin_row["full_name"] or admin_row["username"],
            "username":     admin_row["username"],
            "email":        None,
            "labor_vendor": None,
            "asp_name":     "All ASPs (Superadmin)",
        })

    return jsonify({"error": "Invalid username or password."}), 401


# ── Stats ─────────────────────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/mobile/stats", methods=["GET"])
@jwt_required
def mobile_stats():
    """Return WO summary stat counts for the current ASP vendor."""
    from app.services.database.queries import get_wo_summary_stats
    vf    = mobile_vendor_filter()
    stats = get_wo_summary_stats(vendor_filter=vf)
    return jsonify(stats)


# ── In-Prepare Follow-Up ─────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/mobile/in-prepare", methods=["GET"])
@jwt_required
def mobile_in_prepare():
    """In-Prepare Follow-Up — paginated, filtered by vendor."""
    from app.services.database.queries import get_asp_in_prepare_page
    per_page = min(_int_arg("per_page", 20), 100)
    return jsonify(get_asp_in_prepare_page(
        search         = _str_arg("q"),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = mobile_vendor_filter(),
        prepare_filter = _str_arg("prepare_filter"),
    ))


# ── CCI Follow-Up ────────────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/mobile/cci-followup", methods=["GET"])
@jwt_required
def mobile_cci_followup():
    """CCI Follow-Up — paginated, filtered by vendor."""
    from app.services.database.queries import get_asp_cci_followup_page
    per_page = min(_int_arg("per_page", 20), 100)
    return jsonify(get_asp_cci_followup_page(
        search         = _str_arg("q"),
        followup_state = _str_arg("followup_state"),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = mobile_vendor_filter(),
    ))


# ── Onsite Follow-Up ─────────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/mobile/onsite-followup", methods=["GET"])
@jwt_required
def mobile_onsite_followup():
    """Onsite Follow-Up — paginated, filtered by vendor."""
    from app.services.database.queries import get_asp_onsite_followup_page
    per_page = min(_int_arg("per_page", 20), 100)
    return jsonify(get_asp_onsite_followup_page(
        search         = _str_arg("q"),
        followup_state = _str_arg("followup_state"),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = mobile_vendor_filter(),
    ))


# ── Return Part ───────────────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/mobile/return-part", methods=["GET"])
@jwt_required
def mobile_return_part():
    """Return Part Follow-Up — paginated, filtered by vendor."""
    from app.services.database.queries import get_asp_part_return_page
    per_page = min(_int_arg("per_page", 20), 100)
    return jsonify(get_asp_part_return_page(
        search         = _str_arg("q"),
        followup_state = _str_arg("followup_state"),
        page           = _int_arg("page", 1),
        page_size      = per_page,
        vendor_filter  = mobile_vendor_filter(),
    ))


# ── WO Detail ────────────────────────────────────────────────────────────────

@mobile_bp.route("/api/v1/mobile/wo/<int:work_order_id>", methods=["GET"])
@jwt_required
def mobile_wo_detail(work_order_id: int):
    """
    Return full WO detail (wo_summary + wo_details joined) plus all
    part-order lines from wo_product_detail.

    Vendor-gated: an asp/asp_user can only fetch WOs belonging to their
    own labor_vendor_related value.
    """
    from app.services.database.queries import get_wo_detail, get_parts_for_wo
    row = get_wo_detail(work_order_id)
    if not row:
        return jsonify({"error": "Work order not found"}), 404

    vf = mobile_vendor_filter()
    if vf and row.get("labor_vendor_related") != vf:
        return jsonify({"error": "Work order not found"}), 404

    parts = get_parts_for_wo(work_order_id)
    return jsonify({"wo": row, "parts": parts})
