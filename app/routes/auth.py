"""
auth.py — Login / logout routes for the Lenovo ASP portal.

Four user types are supported:
  admin       — credentials checked against admin_users table
  asp         — credentials checked against asp_details table (standalone, no branch switcher)
  asp_master  — credentials checked against asp_master_accounts (multi-branch master account)
  asp_user    — credentials checked against asp_users table (staff under an ASP)

Session keys stored on successful login:
  session["user_id"]            : int | str  primary-key id (asp_master uses masteruser string)
  session["username"]           : str  login username  (asp_user → their tech_id)
  session["role"]               : str  "admin" | "superadmin" | "asp" | "asp_master" | "asp_user"
  session["display_name"]       : str  full_name (admin/asp_user) or service_provider (asp/asp_master)
  session["labor_vendor"]       : str | None  labor_vendor_related from asp_details (asp / asp_user)
  session["parent_group"]       : str | None  parent_group for asp_master accounts
  session["is_hq_with_branches"]: bool  True only for asp_master role
"""

import re
from functools import wraps
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash,
)
from app.services.database.db import get_db

auth_bp = Blueprint("auth", __name__)


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    """Redirect to login if no active session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Redirect to login if not an admin-role user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.path))
        if session.get("role") not in ("admin", "superadmin"):
            flash("You do not have permission to access that page.", "danger")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in → go straight to the right portal
    if "user_id" in session:
        if session.get("role") in ("asp", "asp_user", "asp_master"):
            return redirect(url_for("asp.dashboard"))
        return redirect(url_for("admin.dashboard"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            error = "Please enter both username and password."
        else:
            conn = get_db()

            # ── 1. Try admin_users first ──────────────────────────────────────
            admin_row = conn.execute(
                "SELECT id, username, password, full_name, role, is_active "
                "FROM admin_users WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()

            if admin_row and str(admin_row["password"]) == password:
                if not admin_row["is_active"]:
                    error = "Your account is disabled. Contact your administrator."
                else:
                    session.clear()
                    session["user_id"]      = admin_row["id"]
                    session["username"]     = admin_row["username"]
                    session["role"]         = admin_row["role"] or "admin"
                    session["display_name"] = admin_row["full_name"] or admin_row["username"]
                    session["labor_vendor"] = None
                    next_url = request.form.get("next") or url_for("admin.dashboard")
                    return redirect(next_url)

            else:
                # ── 2. Try asp_master_accounts ───────────────────────────────
                master_row = conn.execute(
                    "SELECT parent_group, masteruser, password "
                    "FROM asp_master_accounts "
                    "WHERE LOWER(masteruser) = LOWER(?)",
                    (username,),
                ).fetchone()

                if master_row and str(master_row["password"]) == password:
                    pg = master_row["parent_group"]

                    # Default selected office = the ASP HQ of this parent_group
                    hq_row = conn.execute(
                        "SELECT username, service_provider, labor_vendor_related, kota "
                        "FROM asp_details "
                        "WHERE parent_group = ? AND office_type = 'ASP HQ' "
                        "LIMIT 1",
                        (pg,),
                    ).fetchone()

                    session.clear()
                    session["user_id"]              = master_row["masteruser"]
                    session["role"]                 = "asp_master"
                    session["parent_group"]         = pg
                    session["is_hq_with_branches"]  = True

                    if hq_row:
                        # Scope to HQ by default; store master identity in original_*
                        # so the switcher can restore back to unscoped (all-group) view
                        session["username"]              = hq_row["username"]
                        session["display_name"]          = hq_row["service_provider"] or hq_row["username"]
                        session["labor_vendor"]          = hq_row["labor_vendor_related"]
                        session["office_kota"]           = hq_row["kota"] or ""
                        session["original_username"]     = master_row["masteruser"]
                        session["original_display_name"] = pg
                        session["original_labor_vendor"] = None
                        session["original_office_kota"]  = ""
                    else:
                        # No HQ found — fall back to unscoped (shows all WOs in group)
                        session["username"]     = master_row["masteruser"]
                        session["display_name"] = pg
                        session["labor_vendor"] = None
                        session["office_kota"]  = ""

                    next_url = request.form.get("next") or url_for("asp.dashboard")
                    return redirect(next_url)

                else:
                    # ── 3. Try asp_details ────────────────────────────────────
                    # Match on username, vendor_code, or labor_vendor_related
                    asp_row = conn.execute(
                        "SELECT id, username, password, service_provider, labor_vendor_related "
                        "FROM asp_details "
                        "WHERE LOWER(username) = LOWER(?)"
                        "   OR LOWER(vendor_code) = LOWER(?)"
                        "   OR labor_vendor_related = ?",
                        (username, username, username),
                    ).fetchone()

                    if asp_row and str(asp_row["password"]) == password:
                        # All asp accounts are now standalone — no branch switcher
                        session.clear()
                        session["user_id"]             = asp_row["id"]
                        session["username"]            = asp_row["username"]
                        session["role"]                = "asp"
                        session["display_name"]        = asp_row["service_provider"] or asp_row["username"]
                        session["labor_vendor"]        = asp_row["labor_vendor_related"]
                        session["is_hq_with_branches"] = False
                        next_url = request.form.get("next") or url_for("asp.dashboard")
                        return redirect(next_url)

                    else:
                        # ── 4. Try asp_users ─────────────────────────────────
                        # Match on email, phone_number, or tech_id
                        asp_user_row = conn.execute(
                            "SELECT u.id, u.tech_id, u.full_name, u.email, u.password, "
                            "       u.is_active, u.labor_vendor_related "
                            "FROM asp_users u "
                            "WHERE LOWER(u.email) = LOWER(?)"
                            "   OR u.phone_number = ?"
                            "   OR (u.tech_id IS NOT NULL AND LOWER(u.tech_id) = LOWER(?))",
                            (username, username, username),
                        ).fetchone()

                        if asp_user_row and str(asp_user_row["password"]) == password:
                            if not asp_user_row["is_active"]:
                                error = "Your account is disabled. Contact your ASP administrator."
                            else:
                                session.clear()
                                session["user_id"]      = asp_user_row["id"]
                                # username = tech_id so vendor filter works
                                session["username"]     = asp_user_row["tech_id"]
                                session["role"]         = "asp_user"
                                session["display_name"] = asp_user_row["full_name"] or asp_user_row["email"]
                                session["labor_vendor"] = asp_user_row["labor_vendor_related"]
                                session["tech_id"]      = asp_user_row["tech_id"]
                                # keep the user's own email accessible for the profile page
                                session["asp_user_email"] = asp_user_row["email"]
                                next_url = request.form.get("next") or url_for("asp.dashboard")
                                return redirect(next_url)
                        else:
                            error = "Invalid username or password."

    next_url = request.args.get("next", "")
    return render_template(
        "admin/user_management/login.html",
        error=error,
        next=next_url,
    )


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile", methods=["GET"])
def profile():
    """User profile page — works for admin, asp, asp_master, and asp_user roles."""
    if "user_id" not in session:
        return redirect(url_for("auth.login", next="/profile"))
    conn  = get_db()
    role  = session.get("role", "")
    uid   = session.get("user_id")
    user  = {}

    if role in ("admin", "superadmin"):
        row = conn.execute(
            "SELECT username, full_name, email, role, is_active, created_at "
            "FROM admin_users WHERE id = ?", (uid,)
        ).fetchone()
        if row:
            user = dict(row)
            user["display_name"] = row["full_name"] or row["username"]
            # Show "superadmin" as the display username for superadmin role
            if row["role"] == "superadmin":
                user["username"] = "superadmin"

    elif role == "asp_user":
        row = conn.execute(
            "SELECT id, tech_id, labor_vendor_related, full_name, email, phone_number, is_active, created_at "
            "FROM asp_users WHERE id = ?", (uid,)
        ).fetchone()
        if row:
            user = dict(row)
            user["display_name"] = row["full_name"] or row["email"]
            user["username"]     = row["email"]   # shown as the login identifier
            # Fetch the parent ASP details via labor_vendor_related FK
            asp_detail_row = conn.execute(
                "SELECT service_provider, vendor_code, labor_vendor_related, "
                "store_name, kota, island, address, phone_number, "
                "operational_status, operation_support, working_hours "
                "FROM asp_details WHERE labor_vendor_related = ?",
                (row["labor_vendor_related"],)
            ).fetchone()
            if asp_detail_row:
                asp_detail = dict(asp_detail_row)
                wh = asp_detail.get("working_hours") or ""
                if wh:
                    # Insert newline before each day name that follows AM/PM (with optional comma)
                    wh = re.sub(
                        r'(?<=[AP]M)\s*,?\s*(?=(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
                        '\n', wh, flags=re.IGNORECASE
                    )
                    # Also handle comma-only separators (no trailing AM/PM before next day)
                    wh = re.sub(
                        r',\s*(?=(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
                        '\n', wh, flags=re.IGNORECASE
                    )
                    asp_detail["working_hours"] = wh.strip()
                user["asp_detail"] = asp_detail
            else:
                user["asp_detail"] = {}

    elif role == "asp_master":
        row = conn.execute(
            "SELECT parent_group, masteruser, total_associated_asp "
            "FROM asp_master_accounts WHERE masteruser = ?", (uid,)
        ).fetchone()
        if row:
            user = dict(row)
            user["display_name"] = row["parent_group"]
            user["username"]     = row["masteruser"]
            user["pw_request_pending"] = False

    else:  # asp
        row = conn.execute(
            "SELECT username, password, service_provider, vendor_code, labor_vendor_related, "
            "store_name, kota, address, phone_number, working_hours, "
            "operational_status, island, operation_support "
            "FROM asp_details WHERE id = ?", (uid,)
        ).fetchone()
        if row:
            user = dict(row)
            user["display_name"] = row["service_provider"] or row["username"]
            # Check for a pending password change request
            pending = conn.execute(
                "SELECT id FROM asp_pw_change_requests "
                "WHERE asp_username=? AND status='pending' LIMIT 1",
                (row["username"],)
            ).fetchone()
            user["pw_request_pending"] = pending is not None

    user["role"] = role

    # Load ASP users for the ASP profile card (parent ASP only)
    asp_users = []
    if role == "asp" and user.get("labor_vendor_related"):
        asp_user_rows = conn.execute(
            "SELECT id, tech_id, full_name, email, password, phone_number, is_active, created_at "
            "FROM asp_users WHERE labor_vendor_related = ? ORDER BY id",
            (user["labor_vendor_related"],)
        ).fetchall()
        asp_users = [dict(r) for r in asp_user_rows]

    # Load all OTHER superadmin accounts (excluding self) for the superadmin profile card
    superadmin_users = []
    if role == "superadmin":
        sa_rows = conn.execute(
            "SELECT id, username, full_name, email, is_active, created_at "
            "FROM admin_users WHERE role = 'superadmin' AND id != ? ORDER BY id",
            (uid,)
        ).fetchall()
        superadmin_users = [dict(r) for r in sa_rows]

    portal = "admin" if role in ("admin", "superadmin") else "asp"
    return render_template("profile.html", user=user, portal=portal,
                           active_page="profile", asp_users=asp_users,
                           superadmin_users=superadmin_users)
