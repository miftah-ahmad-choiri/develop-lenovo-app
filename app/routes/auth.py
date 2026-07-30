"""
auth.py — Login / logout routes for the Lenovo ASP portal.

Two user types are supported:
  admin  — credentials checked against admin_users table
  asp    — credentials checked against asp_details table

Session keys stored on successful login:
  session["user_id"]            : int  primary-key id
  session["username"]           : str  login username
  session["role"]               : str  "admin" | "asp"
  session["display_name"]       : str  full_name (admin) or service_provider (asp)
  session["labor_vendor"]       : str | None  asp_details.labor_vendor_related (asp only)
"""

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
        if session.get("role") == "asp":
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
                    "SELECT id, username, password, service_provider, labor_vendor_related "
                    "FROM asp_details WHERE LOWER(username) = LOWER(?)",
                    (username,),
                ).fetchone()

                if asp_row and str(asp_row["password"]) == password:
                    session.clear()
                    session["user_id"]      = asp_row["id"]
                    session["username"]     = asp_row["username"]
                    session["role"]         = "asp"
                    session["display_name"] = asp_row["service_provider"] or asp_row["username"]
                    session["labor_vendor"] = asp_row["labor_vendor_related"]
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
