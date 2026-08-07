"""
auth.py — Login / logout routes for the Lenovo ASP portal.

Three user types are supported:
  admin     — credentials checked against admin_users table
  asp       — credentials checked against asp_details table
  asp_user  — credentials checked against asp_users table (staff under an ASP)

Session keys stored on successful login:
  session["user_id"]            : int  primary-key id
  session["username"]           : str  login username  (asp_user → parent asp_username)
  session["role"]               : str  "admin" | "asp" | "asp_user"
  session["display_name"]       : str  full_name (admin/asp_user) or service_provider (asp)
  session["labor_vendor"]       : str | None  labor_vendor_related from asp_details (asp / asp_user)
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
        if session.get("role") in ("asp", "asp_user"):
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
                # ── 2. Try asp_details ────────────────────────────────────────
                asp_row = conn.execute(
                    "SELECT id, username, password, service_provider, labor_vendor_related, "
                    "       office_type, parent_group "
                    "FROM asp_details WHERE LOWER(username) = LOWER(?)",
                    (username,),
                ).fetchone()

                if asp_row and str(asp_row["password"]) == password:
                    # Determine if this ASP is an HQ with sibling branch offices
                    is_hq_with_branches = False
                    if asp_row["office_type"] == "ASP HQ" and asp_row["parent_group"]:
                        branch_count = conn.execute(
                            "SELECT COUNT(*) FROM asp_details "
                            "WHERE parent_group = ? AND office_type = 'ASP Branch'",
                            (asp_row["parent_group"],),
                        ).fetchone()[0]
                        is_hq_with_branches = branch_count > 0

                    session.clear()
                    session["user_id"]            = asp_row["id"]
                    session["username"]           = asp_row["username"]
                    session["role"]               = "asp"
                    session["display_name"]       = asp_row["service_provider"] or asp_row["username"]
                    session["labor_vendor"]       = asp_row["labor_vendor_related"]
                    session["is_hq_with_branches"] = is_hq_with_branches
                    next_url = request.form.get("next") or url_for("asp.dashboard")
                    return redirect(next_url)

                else:
                    # ── 3. Try asp_users (staff accounts under an ASP) ────────
                    # asp_users log in with their email address as the identifier
                    asp_user_row = conn.execute(
                        "SELECT u.id, u.asp_username, u.full_name, u.email, u.password, "
                        "       u.is_active, d.labor_vendor_related "
                        "FROM asp_users u "
                        "JOIN asp_details d ON d.username = u.asp_username "
                        "WHERE LOWER(u.email) = LOWER(?)",
                        (username,),
                    ).fetchone()

                    if asp_user_row and str(asp_user_row["password"]) == password:
                        if not asp_user_row["is_active"]:
                            error = "Your account is disabled. Contact your ASP administrator."
                        else:
                            session.clear()
                            session["user_id"]      = asp_user_row["id"]
                            # username = parent ASP username so vendor filter works
                            session["username"]     = asp_user_row["asp_username"]
                            session["role"]         = "asp_user"
                            session["display_name"] = asp_user_row["full_name"] or asp_user_row["email"]
                            session["labor_vendor"] = asp_user_row["labor_vendor_related"]
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
    """User profile page — works for admin, asp, and asp_user roles."""
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
            "SELECT id, asp_username, full_name, email, phone_number, is_active, created_at "
            "FROM asp_users WHERE id = ?", (uid,)
        ).fetchone()
        if row:
            user = dict(row)
            user["display_name"] = row["full_name"] or row["email"]
            user["username"]     = row["email"]   # shown as the login identifier
            # Fetch the parent ASP details to show in the ASP Information section
            asp_detail_row = conn.execute(
                "SELECT service_provider, vendor_code, labor_vendor_related, "
                "store_name, kota, island, address, phone_number, "
                "operational_status, operation_support, working_hours "
                "FROM asp_details WHERE username = ?",
                (row["asp_username"],)
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
    if role == "asp" and user.get("username"):
        asp_user_rows = conn.execute(
            "SELECT id, full_name, email, password, phone_number, is_active, created_at "
            "FROM asp_users WHERE asp_username = ? ORDER BY id",
            (user["username"],)
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
