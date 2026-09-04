import os
import click
from datetime import datetime
from flask import Flask, redirect, url_for
from flask_cors import CORS
from app.config import Config
from app.routes.excel_upload import excel_upload_bp
from app.routes.asp import asp_bp
from app.routes.admin import admin_bp
from app.routes.auth import auth_bp
from app.routes.api_mobile import mobile_bp


def create_app():
    app = Flask(__name__, template_folder="templates")
    app.config.from_object(Config)

    # Ensure runtime directories exist
    os.makedirs(app.config["UPLOAD_FOLDER"],       exist_ok=True)  # files/upload/
    os.makedirs(app.config["EXCEL_UPLOAD_FOLDER"], exist_ok=True)  # files/upload/excel/
    os.makedirs(app.config["EXCELS_DIR"],          exist_ok=True)  # files/download/excel/
    os.makedirs(app.config["UPLOAD_META_FOLDER"],  exist_ok=True)  # app/templates/admin/upload_meta/
    os.makedirs(app.config["REPORT_DIR"],          exist_ok=True)  # files/report/

    # ── Database setup ─────────────────────────────────────────────────────────
    from app.services.database.migrate import run_migrations
    from app.services.database.db import close_db
    run_migrations(app)
    app.teardown_appcontext(close_db)

    # ── CLI: flask seed-db ─────────────────────────────────────────────────────
    @app.cli.command("seed-db")
    def seed_db_command():
        """Populate the database from the source-db Excel files (run once)."""
        from app.services.database.seed import seed_from_source_db
        click.echo("Seeding database from files/source-db/ …")
        counts = seed_from_source_db(app)
        click.echo("Done.")
        click.echo(f"  wo_summary               : {counts['wo_summary']:,} rows")
        click.echo(f"  wo_details               : {counts['wo_details']:,} rows")
        click.echo(f"  wo_product_detail (MSD)  : {counts['wo_product_from_msd']:,} rows")
        click.echo(f"  wo_product_detail (Ship) : {counts['wo_product_from_shipment']:,} rows processed")
        click.echo(f"  asp_details              : {counts['asp_details']:,} rows")
        click.echo(f"  admin_users              : {counts.get('admin_users', 0):,} rows")

    # ── Template filters ───────────────────────────────────────────────────────
    @app.template_filter("thousands")
    def thousands_filter(value):
        """Format an integer with dot-separated thousands: 21855 → 21.855"""
        try:
            return f"{int(value):,}".replace(",", ".")
        except (ValueError, TypeError):
            return value

    # ── Global template context (available in every template) ─────────────────
    @app.before_request
    def make_session_permanent():
        from flask import session as _sess
        _sess.permanent = True

    @app.context_processor
    def inject_globals():
        from flask import session as _sess
        role                = _sess.get("role", "")
        is_hq_with_branches = _sess.get("is_hq_with_branches", False)
        branch_members      = []

        # asp_master: load all ASPs in the master's parent_group directly from session.
        if role == "asp_master":
            try:
                from app.services.database.db import get_db
                pg = _sess.get("parent_group", "")
                if pg:
                    rows = get_db().execute(
                        """
                        SELECT username, service_provider, kota, office_type
                        FROM asp_details
                        WHERE parent_group = ?
                        ORDER BY service_provider COLLATE NOCASE
                        """,
                        (pg,),
                    ).fetchall()
                    branch_members = [dict(r) for r in rows]
            except Exception:
                branch_members = []

        return {
            "now":                         datetime.utcnow(),
            "session_role":                role,
            "session_username":            _sess.get("username", ""),
            "session_display_name":        _sess.get("display_name", ""),
            "session_is_hq_with_branches": is_hq_with_branches,
            "session_branch_members":      branch_members,
            "session_office_kota":         _sess.get("office_kota", ""),
        }

    # ── CORS for mobile API (JWT-authenticated, origin-agnostic) ──────────────
    CORS(app, resources={r"/api/v1/*": {"origins": "*"}})

    # Register blueprints
    app.register_blueprint(auth_bp)          # /login, /logout
    app.register_blueprint(excel_upload_bp)  # legacy: /upload-excel (kept for backward compat)
    app.register_blueprint(asp_bp)           # /asp/*
    app.register_blueprint(admin_bp)         # /admin/*
    app.register_blueprint(mobile_bp)        # /api/v1/* (mobile JWT API)

    # Root → login page
    @app.route("/")
    def root():
        return redirect(url_for("auth.login"))

    # ── Monday.com auto-scheduler ──────────────────────────────────────────────
    # Start exactly once — the WERKZEUG_RUN_MAIN guard prevents a double-start
    # when Flask's dev-mode reloader forks a child process.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "false":
        from app.routes.admin import (
            start_sync_scheduler,
            _msd_start_startup_scheduler,
            _msd_start_930_watchdog,
            start_resolve_scheduler,
        )
        start_sync_scheduler(app)
        _msd_start_startup_scheduler(app)
        _msd_start_930_watchdog(app)
        start_resolve_scheduler(app)

    return app
