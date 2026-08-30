"""
jwt_service.py — JWT token generation and verification for the mobile API.

Uses PyJWT.  The secret is read from the JWT_SECRET environment variable;
falls back to a hard-coded dev value when the variable is not set.

Usage
-----
    from app.services.jwt_service import generate_token, jwt_required, mobile_vendor_filter

    # In the login endpoint:
    token = generate_token(user_id, username, role, labor_vendor, display_name)

    # As a route decorator (replaces @login_required for mobile routes):
    @jwt_required
    def my_route():
        # g.jwt contains the decoded payload
        user_id = g.jwt["sub"]
"""

import os
import datetime
from functools import wraps

import jwt
from flask import request, jsonify, g


_SECRET = os.environ.get("JWT_SECRET", "lenovo-asp-mobile-dev-secret-change-in-prod")
_ALGO   = "HS256"
_TTL    = datetime.timedelta(hours=24)


def generate_token(
    user_id: int,
    username: str,
    role: str,
    labor_vendor: str | None,
    display_name: str,
    tech_id: str | None = None,
) -> str:
    """Return a signed JWT string valid for 24 hours."""
    payload = {
        "sub":          str(user_id),
        "username":     username,
        "role":         role,
        "labor_vendor": labor_vendor,
        "display_name": display_name,
        "tech_id":      tech_id,
        "exp": datetime.datetime.now(datetime.timezone.utc) + _TTL,
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGO)


def jwt_required(f):
    """Decorator: verify Bearer token, populate g.jwt, else return 401."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized — missing token"}), 401
        token = auth[7:]
        try:
            g.jwt = jwt.decode(token, _SECRET, algorithms=[_ALGO])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired — please log in again"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


def mobile_vendor_filter() -> str | None:
    """
    Return the labor_vendor_related value from the JWT payload for asp/asp_master.
    asp_user is intentionally excluded — use mobile_tech_id_filter() instead.
    """
    role = g.jwt.get("role", "")
    if role == "asp":
        return g.jwt.get("labor_vendor") or None
    return None


def mobile_tech_id_filter() -> str | None:
    """
    Return the tech_id from the JWT payload for asp_user sessions only.
    When set, every WO query is narrowed to WOs assigned to this technician
    (wo_details.tech_id), so technicians cannot see each other's WOs.
    """
    role = g.jwt.get("role", "")
    if role == "asp_user":
        return g.jwt.get("tech_id") or None
    return None
