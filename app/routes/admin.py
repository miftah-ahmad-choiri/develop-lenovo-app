import logging
import os
import re as _re
import sqlite3
import tempfile
import threading
import queue as _queue
import time as _time
import uuid as _uuid
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, current_app, send_file, jsonify,
)
from werkzeug.utils import secure_filename
from app.services.database.db import get_db
from app.services.upload.excel import allowed_excel, save_excel_upload, list_excel_uploads
from app.services.upload.upload_verification import verify_uploaded_file
from app.services.upload.meta_cache import (
    write_meta, read_meta, delete_meta, mark_upserted,
    read_active_open_wos, rebuild_active_open_wos,
    read_incomplete_prev_shipments, rebuild_incomplete_prev_shipments,
    write_wo_product_mismatch, read_wo_product_mismatch,
)
from app.services.upload.excel_to_df import (
    load_single_dataframe, DF_LABELS, _KEY_TO_DF,
)
from app.config.file_categories import FILE_CATEGORY_CONFIGS
from app.services.database.queries import get_wo_summary_stats
from app.services.database.upsert import dispatch_upsert

admin_bp = Blueprint("admin", __name__)

# Reverse map: file_category display string → category_key (e.g. "WOID")
_FILE_CATEGORY_TO_KEY = {
    cfg["file_category"]: key
    for key, cfg in FILE_CATEGORY_CONFIGS.items()
}.get


# ── Auth guard: all admin routes require admin role ───────────────────────────

@admin_bp.before_request
def _require_admin():
    from flask import session as _session
    if "user_id" not in _session:
        return redirect(url_for("auth.login", next=request.path))
    if _session.get("role") not in ("admin", "superadmin"):
        return redirect(url_for("auth.login"))


# ── Dashboard ────────────────────────────────────────────────────────────────

@admin_bp.route("/admin/dashboard", methods=["GET"])
def dashboard():
    stats = get_wo_summary_stats()
    return render_template("admin/dashboard.html",
                           portal="admin", active_page="admin_dashboard",
                           **stats)


# ── API: WO Summary (server-side pagination + search) ────────────────────────

@admin_bp.route("/admin/api/wo-summary", methods=["GET"])
def api_wo_summary():
    """
    JSON endpoint for the paginated WO Summary table.

    Query params:
        q        - free-text search (WO ID, serial, contact, customer, case)
        status   - status filter keyword
        wo_type  - work_order_type filter
        page     - 1-based page number (default 1)
        per_page - rows per page (default 25, max 100)
    """
    from app.services.database.queries import get_wo_summary_page
    per_page = min(int(request.args.get("per_page", 25)), 100)
    result   = get_wo_summary_page(
        search              = request.args.get("q", "").strip(),
        status_filter       = request.args.get("status", "").strip(),
        type_filter         = request.args.get("wo_type", "").strip(),
        case_status_filter  = request.args.get("case_status", "").strip(),
        page                = max(1, int(request.args.get("page", 1))),
        page_size           = per_page,
    )
    return jsonify(result)


# ── API: WO Detail (single WO, on-demand) ────────────────────────────────────

@admin_bp.route("/admin/api/wo-detail/<int:work_order_id>", methods=["GET"])
def api_wo_detail(work_order_id: int):
    """
    JSON endpoint returning the full detail row for one WO
    (wo_summary + wo_details joined).
    """
    from app.services.database.queries import get_wo_detail
    row = get_wo_detail(work_order_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row)


# ── API: Part Order Details for a single WO ──────────────────────────────────

@admin_bp.route("/admin/api/wo-parts/<int:work_order_id>", methods=["GET"])
def api_wo_parts(work_order_id: int):
    """Return all part-order lines for one WO from wo_product_detail."""
    from app.services.database.queries import get_parts_for_wo
    rows = get_parts_for_wo(work_order_id)
    return jsonify(rows)


# ── API: Related WOs by serial number ────────────────────────────────────────

@admin_bp.route("/admin/api/wo-related-serial/<int:work_order_id>", methods=["GET"])
def api_wo_related_serial(work_order_id: int):
    """Return all WOs (including the current one) that share the same serial_number."""
    from app.services.database.queries import get_wo_detail, get_wo_by_serial
    detail = get_wo_detail(work_order_id)
    if not detail or not detail.get("serial_number"):
        return jsonify({"serial_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_serial(detail["serial_number"])
    return jsonify({"serial_number": detail["serial_number"], "current_wo_id": work_order_id, "rows": rows})


# ── API: All WOs by serial number string (admin monday-data view) ─────────────

@admin_bp.route("/admin/api/sn-history/<path:serial_number>", methods=["GET"])
def api_sn_history(serial_number: str):
    """Return all WOs in wo_summary/wo_details that share the given serial_number."""
    from app.services.database.queries import get_wo_by_serial
    rows = get_wo_by_serial(serial_number.strip())
    return jsonify({"serial_number": serial_number.strip(), "rows": rows})


# ── API: Ticket history by case number ───────────────────────────────────────

@admin_bp.route("/admin/api/wo-ticket-history/<int:work_order_id>", methods=["GET"])
def api_wo_ticket_history(work_order_id: int):
    """Return all WOs (including the current one) that share the same case_number (ticket)."""
    from app.services.database.queries import get_wo_detail, get_wo_by_case_number
    detail = get_wo_detail(work_order_id)
    if not detail or not detail.get("case_number"):
        return jsonify({"case_number": None, "current_wo_id": work_order_id, "rows": []})
    rows = get_wo_by_case_number(detail["case_number"])
    return jsonify({"case_number": detail["case_number"], "current_wo_id": work_order_id, "rows": rows})


# ── Ticket Management ────────────────────────────────────────────────────────

@admin_bp.route("/admin/tickets", methods=["GET"])
def tickets():
    return render_template("admin/ticket_management.html",
                           portal="admin", active_page="admin_tickets")


# ── Data Import / Export ─────────────────────────────────────────────────────

@admin_bp.route("/admin/data-import", methods=["GET"])
def data_import():
    import traceback as _tb
    files = list_excel_uploads()
    upload_folder = current_app.config["EXCEL_UPLOAD_FOLDER"]
    meta_folder   = current_app.config["UPLOAD_META_FOLDER"]
    for f in files:
        f["modified_fmt"] = datetime.fromtimestamp(f["modified"]).strftime("%Y-%m-%d %H:%M")
        # Read metadata from the cheap JSON sidecar written at upload time.
        # Fall back to live verification only when the sidecar is missing
        # (e.g. files uploaded before this change was deployed).
        meta = read_meta(meta_folder, f["name"])
        if meta is None:
            try:
                filepath = os.path.join(upload_folder, f["name"])
                meta = verify_uploaded_file(filepath)
                # Backfill the sidecar so the next load is fast
                write_meta(meta_folder, f["name"], meta)
            except Exception:
                current_app.logger.warning(
                    "verify_uploaded_file failed for %s:\n%s", f["name"], _tb.format_exc()
                )
                meta = {}
        f["file_category"]     = meta.get("file_category") or ""
        f["source_file"]       = meta.get("source_file") or ""
        f["latest_date"]       = meta.get("latest_date") or ""
        f["days_range"]        = meta.get("days_range") or ""
        f["validation_status"] = meta.get("validation_status") or ""
        f["upserted"]          = bool(meta.get("upserted"))

    # Build a lookup: file_category → uploaded file dict (last one wins per category)
    uploaded_by_category = {}
    for f in files:
        if f["file_category"]:
            uploaded_by_category[f["file_category"]] = f

    # One row per known category — merged with uploaded file data if present
    category_rows = []
    for key, cfg in FILE_CATEGORY_CONFIGS.items():
        cat_name = cfg["file_category"]
        uploaded = uploaded_by_category.get(cat_name)
        category_rows.append({
            "category_key":  key,
            "file_category": cat_name,
            "source_file":   cfg["source_file"],
            # from uploaded file (empty strings when nothing uploaded yet)
            "filename":      uploaded["name"]                          if uploaded else "",
            "size_kb":       uploaded["size_kb"]                       if uploaded else "",
            "modified_fmt":  uploaded["modified_fmt"]                  if uploaded else "",
            "latest_date":   uploaded["latest_date"]                   if uploaded else "",
            "days_range":    uploaded["days_range"]                    if uploaded else "",
            "upserted":      bool(uploaded.get("upserted"))            if uploaded else False,
        })

    # ── Incomplete previous-month shipments — read from JSON cache ────────────
    incomplete_prev_shipments = read_incomplete_prev_shipments(meta_folder)

    # ── Active open WOs — read from JSON cache (no DB hit on GET) ────────────
    # The cache is rebuilt after every WOID upsert.  On first visit (before any
    # upsert has ever run) the cache file does not exist yet, so we fall back to
    # a one-time bootstrap that also filters by any currently uploaded WOID file.
    active_open_wos = read_active_open_wos(meta_folder)
    if not active_open_wos and not os.path.isfile(
        os.path.join(meta_folder, "active_open_wos.json")
    ):
        # Cache has never been written — bootstrap it now, honouring the
        # currently uploaded WOID file's WO IDs as the exclusion set.
        try:
            import io as _io_b, pandas as _pd_b
            from app.services.upload.upload_verification import (
                verify_uploaded_file as _vuf_b,
            )
            _woid_cfg = FILE_CATEGORY_CONFIGS.get("WOID", {})
            _woid_cat = _woid_cfg.get("file_category", "")
            _bootstrap_ids: set[int] = set()
            for _fname in os.listdir(upload_folder):
                from app.services.upload.excel import allowed_excel as _ae_b
                if not _ae_b(_fname):
                    continue
                _m = read_meta(meta_folder, _fname)
                if _m and _m.get("file_category") == _woid_cat:
                    try:
                        _fp = os.path.join(upload_folder, _fname)
                        _vr_b = _vuf_b(_fp)
                        _sn_b = _vr_b.get("sheet_name", "")
                        with open(_fp, "rb") as _fh_b:
                            _fb_b = _io_b.BytesIO(_fh_b.read())
                        _df_b = (
                            _pd_b.read_excel(_fb_b, sheet_name=_sn_b)
                            if _sn_b else _pd_b.read_excel(_fb_b)
                        )
                        _bootstrap_ids = {
                            int(float(str(v)))
                            for v in _df_b.get(
                                "Work Order ID", _pd_b.Series(dtype=object)
                            )
                            if v is not None
                            and str(v).strip() not in ("", "nan", "NaN")
                        }
                    except Exception:
                        pass
                    break  # only one WOID file
            active_open_wos = rebuild_active_open_wos(
                meta_folder,
                current_app.config["DATABASE_PATH"],
                _bootstrap_ids or None,
            )
        except Exception:
            current_app.logger.warning(
                "active_open_wos bootstrap failed:\n" + _tb.format_exc()
            )

    # ── WO-product mismatch — read from JSON cache ────────────────────────────
    wo_product_mismatch = read_wo_product_mismatch(meta_folder)

    return render_template("admin/data_import.html",
                           files=files,
                           category_rows=category_rows,
                           active_open_wos=active_open_wos,
                           incomplete_prev_shipments=incomplete_prev_shipments,
                           wo_product_mismatch=wo_product_mismatch,
                           portal="admin", active_page="data_import",
                           active_group="data_import_export")


@admin_bp.route("/admin/msd-wo-updates", methods=["GET"])
def msd_wo_updates():
    project_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    script_path = os.path.join(
        project_root,
        "app",
        "scripts",
        "msd-auto-download",
        "msd-auto-download.py",
    )
    downloads_dir = os.path.join(project_root, "files", "msd-auto-download")

    files = []
    if os.path.isdir(downloads_dir):
        for name in sorted(
            os.listdir(downloads_dir),
            key=lambda item: os.path.getmtime(os.path.join(downloads_dir, item)),
            reverse=True,
        ):
            file_path = os.path.join(downloads_dir, name)
            if not os.path.isfile(file_path):
                continue
            files.append({
                "name": name,
                "size_kb": round(os.path.getsize(file_path) / 1024, 1),
                "modified_fmt": datetime.fromtimestamp(
                    os.path.getmtime(file_path)
                ).strftime("%Y-%m-%d %H:%M"),
            })

    with _msd_lock:
        is_running = _msd_thread is not None and _msd_thread.is_alive()

    return render_template(
        "admin/export-import/msd_wo_updates.html",
        portal="admin",
        active_page="msd_wo_updates",
        active_group="data_import_export",
        script_path=script_path,
        downloads_dir=downloads_dir,
        files=files,
        is_running=is_running,
        in_window=_msd_in_active_window(),
        boot_id=_MSD_BOOT_ID,
    )


# A unique ID stamped at process start — used by the frontend to detect server
# restarts and automatically flush stale sessionStorage log content.
_MSD_BOOT_ID: str = _uuid.uuid4().hex

_msd_log_queue: _queue.Queue = _queue.Queue(maxsize=2000)
_msd_thread: threading.Thread | None = None
_msd_lock = threading.Lock()

# OTP handshake — script blocks on _msd_otp_queue.get(); route puts the code in
_msd_otp_queue:    _queue.Queue = _queue.Queue(maxsize=1)
_msd_otp_pending:  bool = False   # True while script is waiting for OTP
_msd_relogin_pending: bool = False  # True while script is waiting for re-login

# ── Startup auto-run ─────────────────────────────────────────────────────────
# 10 minutes after the process starts, auto-trigger the MSD download if not
# already running and the active window is open.
_MSD_STARTUP_DELAY_SEC: int = 10 * 60
_msd_startup_run_at: float = _time.time() + _MSD_STARTUP_DELAY_SEC  # Unix ts


def _msd_startup_scheduler(app) -> None:
    """Wait 10 min then auto-trigger the MSD download exactly once."""
    _time.sleep(_MSD_STARTUP_DELAY_SEC)
    global _msd_thread, _msd_startup_run_at
    _msd_startup_run_at = 0.0   # clear the countdown
    with _msd_lock:
        already_running = _msd_thread is not None and _msd_thread.is_alive()
    if already_running:
        return  # user already started it manually — skip
    if not _msd_in_active_window():
        return  # outside office hours — don't auto-start
    with app.app_context():
        with _msd_lock:
            if _msd_thread is not None and _msd_thread.is_alive():
                return
            _msd_thread = threading.Thread(
                target=_run_msd_download_task,
                args=(app, False),
                daemon=True,
                name="msd-auto-download-startup",
            )
            _msd_thread.start()


def _msd_start_startup_scheduler(app) -> None:
    t = threading.Thread(
        target=_msd_startup_scheduler,
        args=(app,),
        daemon=True,
        name="msd-startup-scheduler",
    )
    t.start()


def _msd_in_active_window() -> bool:
    """Mon–Fri 06:00–20:00 local time."""
    now = datetime.now()
    return now.weekday() < 5 and 6 <= now.hour < 20


# Matches Werkzeug/Flask HTTP access log lines that leak via captured stderr.
# Pattern: '127.0.0.1 - - [DD/Mon/YYYY ...' or the formatted variant with timestamp prefix.
_WERKZEUG_LINE_RE = _re.compile(
    r'(?:^|\s)(?:\d{1,3}\.){3}\d{1,3}\s+-\s+-\s+\[',
)


class _MsdQueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        # Only forward records that originated from the msd_auto_download logger
        if not record.name.startswith("msd_auto_download"):
            return
        # Drop Werkzeug HTTP access log lines that leak via captured stderr
        msg = record.getMessage()
        if _WERKZEUG_LINE_RE.search(msg):
            return
        try:
            _msd_log_queue.put_nowait({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "msg": msg,
            })
        except _queue.Full:
            try:
                _msd_log_queue.get_nowait()
                _msd_log_queue.put_nowait({
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "level": record.levelname,
                    "msg": msg,
                })
            except (_queue.Full, _queue.Empty):
                pass


_msd_queue_handler = _MsdQueueHandler()
_msd_queue_handler.setLevel(logging.INFO)


@admin_bp.route("/admin/msd-wo-updates/trigger", methods=["POST"])
def msd_wo_updates_trigger():
    global _msd_thread

    # Off-hours: allow trigger but run only once, then wait for office hours.
    # In-window: normal repeating loop.
    run_once = not _msd_in_active_window()

    with _msd_lock:
        if _msd_thread is not None and _msd_thread.is_alive():
            return jsonify({"ok": False, "error": "MSD auto-download is already running."}), 409

        app = current_app._get_current_object()
        _msd_thread = threading.Thread(
            target=_run_msd_download_task,
            args=(app, run_once),
            daemon=True,
            name="msd-auto-download",
        )
        _msd_thread.start()

    return jsonify({"ok": True, "run_once": run_once})


@admin_bp.route("/admin/msd-wo-updates/stream", methods=["GET"])
def msd_wo_updates_stream():
    def generate():
        import json as _json
        while True:
            try:
                rec = _msd_log_queue.get(timeout=15)
                yield f"data: {_json.dumps(rec)}\n\n"
            except _queue.Empty:
                yield "data: {\"keepalive\": true}\n\n"

    return current_app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@admin_bp.route("/admin/msd-wo-updates/files", methods=["GET"])
def msd_wo_updates_files():
    project_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    downloads_dir = os.path.join(project_root, "files", "msd-auto-download")
    files = []
    if os.path.isdir(downloads_dir):
        for name in sorted(
            os.listdir(downloads_dir),
            key=lambda item: os.path.getmtime(os.path.join(downloads_dir, item)),
            reverse=True,
        ):
            file_path = os.path.join(downloads_dir, name)
            if not os.path.isfile(file_path):
                continue
            files.append({
                "name": name,
                "size_kb": round(os.path.getsize(file_path) / 1024, 1),
                "modified_fmt": datetime.fromtimestamp(
                    os.path.getmtime(file_path)
                ).strftime("%Y-%m-%d %H:%M"),
            })
    return jsonify({"ok": True, "files": files})


@admin_bp.route("/admin/msd-wo-updates/relogin", methods=["POST"])
def msd_wo_updates_relogin():
    global _msd_relogin_pending
    if not _msd_relogin_pending:
        return jsonify({"ok": False, "error": "No re-login is currently pending."}), 409
    try:
        _msd_otp_queue.put_nowait("__RELOGIN_CONFIRMED__")
        _msd_relogin_pending = False
    except _queue.Full:
        return jsonify({"ok": False, "error": "Re-login already queued."}), 409
    return jsonify({"ok": True})


@admin_bp.route("/admin/msd-wo-updates/status", methods=["GET"])
def msd_wo_updates_status():
    with _msd_lock:
        is_running = _msd_thread is not None and _msd_thread.is_alive()
    # Expose startup countdown: positive Unix ts means auto-start pending
    startup_run_at = _msd_startup_run_at if _msd_startup_run_at > _time.time() else None
    return jsonify({
        "ok": True,
        "is_running": is_running,
        "otp_pending": _msd_otp_pending,
        "relogin_pending": _msd_relogin_pending,
        "in_window": _msd_in_active_window(),
        "startup_run_at": startup_run_at,
    })


@admin_bp.route("/admin/msd-wo-updates/otp", methods=["POST"])
def msd_wo_updates_otp():
    global _msd_otp_pending
    if not _msd_otp_pending:
        return jsonify({"ok": False, "error": "No OTP is currently expected."}), 409
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    if not code:
        return jsonify({"ok": False, "error": "OTP code is required."}), 400
    try:
        _msd_otp_queue.put_nowait(code)
        _msd_otp_pending = False
    except _queue.Full:
        return jsonify({"ok": False, "error": "OTP already queued."}), 409
    return jsonify({"ok": True})


@admin_bp.route("/admin/data-import/verify", methods=["POST"])
def data_import_verify():
    """
    Accepts a multipart file, saves it to a temp location, runs
    verify_uploaded_file(), and returns JSON — no permanent save is done here.
    """
    file = request.files.get("excel_file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file selected."})
    if not allowed_excel(file.filename):
        return jsonify({"ok": False, "error": "Invalid file type. Allowed: .xlsx, .xls, .csv"})

    safe_name = secure_filename(file.filename)
    suffix = "." + safe_name.rsplit(".", 1)[-1].lower()

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            file.save(tmp_path)
    except PermissionError:
        return jsonify({
            "ok": False,
            "filename": safe_name,
            "error": (
                f'The file "{safe_name}" is currently open in another program '
                "(e.g. Microsoft Excel or OneDrive sync). "
                "Please close it and try again."
            ),
        })

    try:
        result = verify_uploaded_file(tmp_path)
        result["filename"] = safe_name          # use the user-visible name
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return jsonify(result)


@admin_bp.route("/admin/data-import/upload", methods=["POST"])
def data_import_upload():
    import shutil, traceback as _tb
    file = request.files.get("excel_file")
    if not file or not file.filename:
        flash("Please choose an Excel file to upload.", "danger")
        return redirect(url_for("admin.data_import"))
    if not allowed_excel(file.filename):
        flash("Invalid file type. Allowed: .xlsx, .xls, .csv", "danger")
        return redirect(url_for("admin.data_import"))

    safe_name = secure_filename(file.filename)
    upload_folder = current_app.config["EXCEL_UPLOAD_FOLDER"]
    meta_folder   = current_app.config["UPLOAD_META_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    # ── Step 1: save to a temp file and verify category ──────────────────────
    suffix = "." + safe_name.rsplit(".", 1)[-1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        file.save(tmp_path)

    try:
        new_result = verify_uploaded_file(tmp_path)
        new_category = new_result.get("file_category") or ""

        # ── Step 2: if category matches an existing file, compare dates ──────
        replaced_name = None
        if new_category:
            for existing_fname in list(os.listdir(upload_folder)):
                if not allowed_excel(existing_fname):
                    continue
                # Use the cheap sidecar instead of re-parsing the whole file
                ex_result = read_meta(meta_folder, existing_fname)
                if ex_result is None:
                    try:
                        ex_result = verify_uploaded_file(
                            os.path.join(upload_folder, existing_fname)
                        )
                    except Exception:
                        continue
                if ex_result.get("file_category") != new_category:
                    continue

                # Same category found — compare latest_date (format dd-mm-yyyy)
                def _parse_date(d):
                    try:
                        from datetime import datetime as _dt
                        return _dt.strptime(d, "%d-%m-%Y")
                    except Exception:
                        return None

                new_date = _parse_date(new_result.get("latest_date") or "")
                ex_date  = _parse_date(ex_result.get("latest_date") or "")

                if new_date and ex_date and new_date >= ex_date:
                    # New file is newer or equal — delete the old one and its sidecar
                    existing_path = os.path.join(upload_folder, existing_fname)
                    os.remove(existing_path)
                    delete_meta(meta_folder, existing_fname)
                    replaced_name = existing_fname
                elif new_date and ex_date and new_date < ex_date:
                    existing_path = os.path.join(upload_folder, existing_fname)
                    # Existing file is newer — reject the upload
                    flash(
                        f'Upload rejected: "{existing_fname}" already covers a '
                        f'later date ({ex_result["latest_date"]}) for category '
                        f'"{new_category}". Delete it first if you want to replace it.',
                        "warning",
                    )
                    return redirect(url_for("admin.data_import"))
                else:
                    # Can't compare dates — keep new, delete old
                    existing_path = os.path.join(upload_folder, existing_fname)
                    os.remove(existing_path)
                    delete_meta(meta_folder, existing_fname)
                    replaced_name = existing_fname
                break  # only one file per category allowed

        # ── Step 3: move temp file to upload folder ───────────────────────────
        dest_path = os.path.join(upload_folder, safe_name)
        shutil.move(tmp_path, dest_path)
        tmp_path = None  # prevent finally from deleting it again

        # ── Step 4: write sidecar so GET loads are instant ───────────────────
        write_meta(meta_folder, safe_name, new_result)

        if replaced_name:
            flash(
                f'"{safe_name}" uploaded and replaced "{replaced_name}" '
                f'(same category: {new_category}).',
                "success",
            )
        else:
            flash(f'File "{safe_name}" uploaded successfully.', "success")

    except Exception:
        current_app.logger.error("data_import_upload ERROR:\n" + _tb.format_exc())
        flash("Upload failed due to an internal error.", "danger")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return redirect(url_for("admin.data_import"))


@admin_bp.route("/admin/data-import/upsert-preview/<category_key>", methods=["GET"])
def data_import_upsert_preview(category_key: str):
    """
    Return JSON preview of rows that will be impacted when the user clicks
    Upsert for *category_key*.

    Logic per category:
      SOID     — excel soid values NOT yet in wo_product_detail
                 (where work_order_id already exists in wo_summary)
      SHIPMENT — excel rows whose SO (work_order_id) matches wo_summary
                 AND whose SOID either does not exist in wo_product_detail
                 OR exists but has ship_pn IS NULL
                 (i.e. records that will receive shipment data for the first time)

    NOTE: WOID preview logic is not implemented yet. The Upsert button for
    Work Order Advance Find View will run the upsert directly without a preview.

    Response JSON:
      {
        "ok": true,
        "category_key": "SHIPMENT",
        "impacted_count": 42,
        "total_excel_rows": 150,
        "date_col": "Order Date",
        "latest_rows": [ { col: val, ... }, ... ],   // top-3 newest
        "oldest_rows": [ { col: val, ... }, ... ],   // top-3 oldest
        "preview_cols": ["col1", "col2", ...]
      }
    """
    import traceback as _tb
    category_key = category_key.upper()
    if category_key not in FILE_CATEGORY_CONFIGS:
        return jsonify({"ok": False, "error": f'Unknown category "{category_key}".'})

    upload_folder = current_app.config["EXCEL_UPLOAD_FOLDER"]
    meta_folder   = current_app.config["UPLOAD_META_FOLDER"]

    # Find the uploaded file for this category
    cat_name = FILE_CATEGORY_CONFIGS[category_key]["file_category"]
    target_file = None
    for fname in os.listdir(upload_folder):
        from app.services.upload.excel import allowed_excel as _allowed
        if not _allowed(fname):
            continue
        m = read_meta(meta_folder, fname)
        if m and m.get("file_category") == cat_name:
            target_file = fname
            break

    if not target_file:
        return jsonify({"ok": False, "error": f'No uploaded file found for category "{cat_name}".'})

    try:
        import io
        import pandas as pd
        from app.services.database.seed import _safe_int, _build_soid
        from app.services.upload.upload_verification import verify_uploaded_file

        filepath  = os.path.join(upload_folder, target_file)
        vresult   = verify_uploaded_file(filepath)
        sheet_name = vresult.get("sheet_name", "")
        ext = filepath.rsplit(".", 1)[-1].lower()

        # Read the file bytes into memory once so openpyxl's file handle
        # (held open by verify_uploaded_file on Windows) does not block the
        # subsequent pd.read_excel call.
        with open(filepath, "rb") as _fh:
            _file_bytes = io.BytesIO(_fh.read())

        if ext == "csv":
            df = pd.read_csv(_file_bytes)
        elif sheet_name:
            df = pd.read_excel(_file_bytes, sheet_name=sheet_name)
        else:
            df = pd.read_excel(_file_bytes)

        db_path = current_app.config["DATABASE_PATH"]
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row

        try:
            if category_key == "WOID":
                # Smart diff preview for WOID.
                # A row qualifies for upsert when ANY of the following is true:
                #   1. work_order_id is not yet in wo_summary or wo_details (NEW row)
                #   2. Any date column in Excel is strictly later than the DB value
                #      (or DB is NULL and Excel has a real value)
                #   3. Any tracked status column has a different value from the DB
                import math as _math
                from datetime import datetime as _dt_woid
                from app.services.database.seed import _to_iso as _woid_to_iso

                _woid_date_cols = {
                    "Created On":                       ("wo_summary", "created_on"),
                    "Committed Delivery Date":          ("wo_summary", "committed_delivery_date"),
                    "Actual Committed Onsite Date":     ("wo_summary", "actual_committed_onsite_date"),
                    "Release Date":                     ("wo_details", "release_date"),
                    "Original Committed Onsite Date":   ("wo_details", "original_committed_onsite_date"),
                    "Customer Defer Date":              ("wo_details", "customer_defer_date"),
                    "Completion Date":                  ("wo_details", "completion_date"),
                    "Closing Date":                     ("wo_details", "closing_date"),
                }
                _woid_status_cols = {
                    "Work Order Status":          ("wo_summary", "work_order_status"),
                    "Case Status (Case) (Case)":  ("wo_summary", "case_status"),
                    "Closing Code":               ("wo_details", "closing_code"),
                    "Repeat Repair Reason":       ("wo_details", "repeat_repair_reason"),
                    "WO Cancellation Reason":     ("wo_details", "wo_cancellation_reason"),
                }

                db_summary_snap = {
                    r[0]: {
                        "created_on":                   r[1],
                        "committed_delivery_date":      r[2],
                        "actual_committed_onsite_date": r[3],
                        "work_order_status":            r[4],
                        "case_status":                  r[5],
                    }
                    for r in db_conn.execute(
                        "SELECT work_order_id, created_on, committed_delivery_date, "
                        "actual_committed_onsite_date, work_order_status, case_status "
                        "FROM wo_summary"
                    ).fetchall()
                }
                db_details_snap = {
                    r[0]: {
                        "release_date":                   r[1],
                        "original_committed_onsite_date": r[2],
                        "customer_defer_date":            r[3],
                        "completion_date":                r[4],
                        "closing_date":                   r[5],
                        "closing_code":                   r[6],
                        "repeat_repair_reason":           r[7],
                        "wo_cancellation_reason":         r[8],
                    }
                    for r in db_conn.execute(
                        "SELECT work_order_id, release_date, original_committed_onsite_date, "
                        "customer_defer_date, completion_date, closing_date, "
                        "closing_code, repeat_repair_reason, wo_cancellation_reason "
                        "FROM wo_details"
                    ).fetchall()
                }

                def _has_woid_val(v) -> bool:
                    if v is None:
                        return False
                    if isinstance(v, float) and _math.isnan(v):
                        return False
                    s = str(v).strip()
                    return s not in ("", "nan", "nat", "none", "null", "NaT")

                def _date_str_woid(v):
                    if not _has_woid_val(v):
                        return None
                    raw = _woid_to_iso(v)
                    return raw[:10] if raw else None

                def _date_newer(excel_val, db_str):
                    ex = _date_str_woid(excel_val)
                    if ex is None:
                        return False
                    if db_str is None:
                        return True
                    try:
                        return _dt_woid.fromisoformat(ex) > _dt_woid.fromisoformat(db_str[:10])
                    except (ValueError, TypeError):
                        return False

                def _status_changed_woid(excel_val, db_val):
                    ex_s = str(excel_val).strip() if _has_woid_val(excel_val) else ""
                    db_s = str(db_val).strip()    if db_val is not None else ""
                    return ex_s != db_s

                def _reason_woid(row) -> str | None:
                    """Return a human-readable upsert reason, or None if the row is skipped."""
                    wo_id = _safe_int(row.get("Work Order ID"))
                    if wo_id is None:
                        return None
                    # Rule 1 — brand new WO
                    if wo_id not in db_summary_snap or wo_id not in db_details_snap:
                        return "New WO"
                    s_row = db_summary_snap[wo_id]
                    d_row = db_details_snap[wo_id]
                    reasons: list[str] = []
                    # Rule 2 — newer date
                    for excel_col, (tbl, db_col) in _woid_date_cols.items():
                        db_val = (s_row if tbl == "wo_summary" else d_row).get(db_col)
                        if _date_newer(row.get(excel_col), db_val):
                            ex_s  = _date_str_woid(row.get(excel_col)) or ""
                            db_s  = db_val[:10] if db_val else "null"
                            reasons.append(f"{excel_col}: {db_s} → {ex_s}")
                    # Rule 3 — status changed
                    for excel_col, (tbl, db_col) in _woid_status_cols.items():
                        db_val = (s_row if tbl == "wo_summary" else d_row).get(db_col)
                        if _status_changed_woid(row.get(excel_col), db_val):
                            ex_s = str(row.get(excel_col, "")).strip() if _has_woid_val(row.get(excel_col)) else "(empty)"
                            db_s = str(db_val).strip() if db_val is not None else "(empty)"
                            reasons.append(f"{excel_col}: \u201c{db_s}\u201d → \u201c{ex_s}\u201d")
                    return "; ".join(reasons) if reasons else None

                def _qualifies_woid(row):
                    return _reason_woid(row) is not None

                wo_id_col    = "Work Order ID"
                date_col     = "Modified On"
                _woid_reason_col = "Reason"
                preview_cols = [
                    _woid_reason_col,
                    "Modified On",
                    "Work Order ID", "Serial Number", "Created On",
                    "Work Order Status", "Case Status (Case) (Case)",
                    "Closing Code", "Repeat Repair Reason", "WO Cancellation Reason",
                    "Completion Date", "Closing Date", "Committed Delivery Date",
                ]

                new_df = df[df.apply(_qualifies_woid, axis=1)].copy() \
                    if wo_id_col in df.columns \
                    else pd.DataFrame()

                # Inject synthetic "Reason" column (first column — explains why each row qualifies)
                # Also rename the raw Excel column "(Do Not Modify) Modified On" → "Modified On"
                # so the display name is clean without changing the filter/sort logic.
                if not new_df.empty:
                    new_df[_woid_reason_col] = new_df.apply(_reason_woid, axis=1).fillna("")
                    if "(Do Not Modify) Modified On" in new_df.columns:
                        new_df = new_df.rename(columns={"(Do Not Modify) Modified On": "Modified On"})

                # ── Active WOs not present in this Excel file ──────────────────
                # Fetch all WOs from the DB that are still "open": no closing_date
                # AND no completion_date.  Then exclude any WO ID that appears in
                # the Excel file (whether impacted or not) — those are already
                # accounted for.  What remains are open WOs that were simply not
                # included in the upload, which may need attention.
                excel_wo_ids = set()
                if wo_id_col in df.columns:
                    excel_wo_ids = {
                        _safe_int(v)
                        for v in df[wo_id_col]
                        if _safe_int(v) is not None
                    }

                active_wo_rows = db_conn.execute(
                    "SELECT s.work_order_id, s.serial_number, s.created_on, "
                    "s.work_order_status, s.case_status, "
                    "d.completion_date, d.closing_date, d.closing_code "
                    "FROM wo_summary s "
                    "LEFT JOIN wo_details d ON d.work_order_id = s.work_order_id "
                    "WHERE (d.completion_date IS NULL OR d.completion_date = '') "
                    "  AND (d.closing_date IS NULL OR d.closing_date = '')"
                ).fetchall()

                def _is_cancelled(wo_status, case_status) -> bool:
                    """True when either status contains 'cancel' (case-insensitive)."""
                    for s in (wo_status or "", case_status or ""):
                        if "cancel" in str(s).lower():
                            return True
                    return False

                active_wo_not_in_excel = [
                    {
                        "Work Order ID":         str(r[0]),
                        "Serial Number":         r[1] or "",
                        "Created On":            r[2] or "",
                        "Work Order Status":     r[3] or "",
                        "Case Status":           r[4] or "",
                        "Completion Date":       r[5] or "",
                        "Closing Date":          r[6] or "",
                        "Closing Code":          r[7] or "",
                    }
                    for r in active_wo_rows
                    if r[0] not in excel_wo_ids
                    and not _is_cancelled(r[3], r[4])
                ]
                active_wo_cols = [
                    "Work Order ID", "Serial Number", "Created On",
                    "Work Order Status", "Case Status",
                    "Completion Date", "Closing Date", "Closing Code",
                ]

            elif category_key == "SOID":
                # Impacted rows qualify when ANY of the following is true for a
                # row whose work_order_id exists in wo_summary:
                #   1. SOID is brand-new (not yet in wo_product_detail)
                #   2. Acceptance Date / Shipment Date / Delivery Date in Excel
                #      is strictly later than the DB value (or DB is NULL and
                #      Excel has a real date)
                #   3. Work Order Product Status has a different value from the DB
                import math as _math_soid
                from datetime import datetime as _dt_soid
                from app.services.database.seed import _to_iso as _soid_to_iso

                valid_wo_ids = {
                    r[0] for r in db_conn.execute(
                        "SELECT work_order_id FROM wo_summary"
                    ).fetchall()
                }
                # Full state snapshot: soid → (acceptance_date, shipment_date,
                #                               delivery_date, wo_product_status)
                db_soid_snap = {
                    r[0]: {
                        "acceptance_date":   r[1],
                        "shipment_date":     r[2],
                        "delivery_date":     r[3],
                        "wo_product_status": r[4],
                    }
                    for r in db_conn.execute(
                        "SELECT soid, acceptance_date, shipment_date, "
                        "delivery_date, wo_product_status "
                        "FROM wo_product_detail"
                    ).fetchall()
                }

                wo_col   = "Work Order"
                line_col = "Line Order"
                date_col = "Modified On"
                _soid_reason_col = "Upsert Reason"
                preview_cols = [
                    _soid_reason_col,
                    "Modified On",
                    wo_col, line_col, "Product", "Description",
                    "Acceptance Date", "Shipment Date", "Delivery Date",
                    "Work Order Product Status",
                ]

                _soid_date_map = [
                    ("Acceptance Date",  "acceptance_date"),
                    ("Shipment Date",    "shipment_date"),
                    ("Delivery Date",    "delivery_date"),
                ]

                def _has_soid_val(v) -> bool:
                    if v is None:
                        return False
                    if isinstance(v, float) and _math_soid.isnan(v):
                        return False
                    s = str(v).strip()
                    return s not in ("", "nan", "nat", "none", "null", "NaT")

                def _soid_date_str(v):
                    if not _has_soid_val(v):
                        return None
                    raw = _soid_to_iso(v)
                    return raw[:10] if raw else None

                def _soid_date_newer(excel_val, db_str):
                    ex = _soid_date_str(excel_val)
                    if ex is None:
                        return False
                    if db_str is None:
                        return True
                    try:
                        return _dt_soid.fromisoformat(ex) > _dt_soid.fromisoformat(db_str[:10])
                    except (ValueError, TypeError):
                        return False

                def _soid_status_changed(excel_val, db_val):
                    ex_s = str(excel_val).strip() if _has_soid_val(excel_val) else ""
                    db_s = str(db_val).strip()    if db_val is not None else ""
                    return ex_s != db_s

                def _qualify_soid(row):
                    """Return (qualifies: bool, reason: str)."""
                    wo_id = _safe_int(row.get(wo_col))
                    ln    = _safe_int(row.get(line_col))
                    soid  = _build_soid(wo_id, ln)
                    if soid is None or wo_id not in valid_wo_ids:
                        return False, ""
                    if soid not in db_soid_snap:
                        return True, "New SOID"
                    db_row = db_soid_snap[soid]
                    reasons = []
                    for excel_col, db_col in _soid_date_map:
                        if _soid_date_newer(row.get(excel_col), db_row.get(db_col)):
                            reasons.append(f"Newer {excel_col}")
                    if _soid_status_changed(
                        row.get("Work Order Product Status"),
                        db_row.get("wo_product_status"),
                    ):
                        reasons.append("Status changed")
                    if reasons:
                        return True, "; ".join(reasons)
                    return False, ""

                if wo_col in df.columns and line_col in df.columns:
                    _soid_quals = df.apply(_qualify_soid, axis=1)
                    _soid_mask  = _soid_quals.apply(lambda x: x[0])
                    new_df      = df[_soid_mask].copy()
                    new_df[_soid_reason_col] = _soid_quals[_soid_mask].apply(lambda x: x[1])
                    # Rename the raw Excel column to a clean display name
                    if "(Do Not Modify) Modified On" in new_df.columns:
                        new_df = new_df.rename(
                            columns={"(Do Not Modify) Modified On": "Modified On"}
                        )
                else:
                    new_df = pd.DataFrame()

                # ── WO-ID mismatch: Excel rows whose Work Order is not in either
                # wo_summary or wo_details.  These rows are silently skipped by the
                # upsert — surface them so the operator can investigate.
                # "valid_wo_ids" is built from wo_summary above; cross-check against
                # wo_details as well for completeness.
                _detail_ids = {
                    r[0] for r in db_conn.execute(
                        "SELECT work_order_id FROM wo_details"
                    ).fetchall()
                }
                _soid_mismatch_rows: list[dict] = []
                if wo_col in df.columns:
                    _seen_mismatch: set[int] = set()
                    for _, _mr in df.iterrows():
                        _mwo = _safe_int(_mr.get(wo_col))
                        if _mwo is None:
                            continue
                        if _mwo not in valid_wo_ids and _mwo not in _detail_ids:
                            if _mwo not in _seen_mismatch:
                                _seen_mismatch.add(_mwo)
                                # Normalise Created On to a plain date string
                                _created_raw = _mr.get("Created On")
                                try:
                                    import pandas as _pd_mm
                                    _created_str = str(_pd_mm.to_datetime(_created_raw).date()) \
                                        if _created_raw is not None and str(_created_raw).strip() not in ("", "nan", "NaT") \
                                        else ""
                                except Exception:
                                    _created_str = str(_created_raw or "").strip()[:10]
                                _soid_mismatch_rows.append({
                                    "Reason":            "WO Not Found",
                                    "Created On":        _created_str,
                                    "Work Order ID":     str(_mwo),
                                    "Line Order":        str(_mr.get(line_col) or ""),
                                    "Product":           str(_mr.get("Product") or ""),
                                    "Description":       str(_mr.get("Description") or ""),
                                    "WO Product Status": str(_mr.get("Work Order Product Status") or ""),
                                })
                # NOTE: _soid_mismatch_rows is NOT written to the JSON cache here.
                # Writing only happens inside data_import_upsert() after the user
                # confirms upsert — so cancelling the modal or refreshing the page
                # leaves the previous cache intact and the page card unchanged.

            elif category_key == "SHIPMENT":
                # Impacted rows = Excel rows where:
                #   1. SO (work_order_id) exists in wo_summary, AND
                #   2. For at least one of (ship_pn, awb, ship_pou_pod_time):
                #        - the DB value is NULL, AND
                #        - the Excel value is non-empty
                # If the Excel value for a NULL DB column is also empty/NaT,
                # there is nothing to write — the row is excluded from the preview.
                import math as _math

                valid_wo_ids = {
                    r[0] for r in db_conn.execute(
                        "SELECT work_order_id FROM wo_summary"
                    ).fetchall()
                }
                # Fetch current DB state for the three key columns, keyed by soid
                db_shipment = {
                    r[0]: {"ship_pn": r[1], "awb": r[2], "ship_pou_pod_time": r[3]}
                    for r in db_conn.execute(
                        "SELECT soid, ship_pn, awb, ship_pou_pod_time FROM wo_product_detail"
                    ).fetchall()
                }
                soid_col = "SOID"
                so_col   = "SO"
                date_col = "Order Date"
                _ship_reason_col = "Upsert Reason"
                preview_cols = [
                    _ship_reason_col,
                    so_col, soid_col, "Ship PN", "Ship PN Desc",
                    date_col, "Company Name", "AWB", "Ship POU POD Time", "SLA",
                ]

                # Excel column name → DB column name for the three tracked fields
                _excel_to_db = {
                    "Ship PN":           "ship_pn",
                    "AWB":               "awb",
                    "Ship POU POD Time": "ship_pou_pod_time",
                }

                def _has_value(v) -> bool:
                    """True when v is a non-empty, non-NaT, non-NaN value."""
                    if v is None:
                        return False
                    if isinstance(v, float) and _math.isnan(v):
                        return False
                    s = str(v).strip()
                    return s != "" and s.lower() not in ("nat", "nan", "none", "null")

                def _qualify_shipment(row):
                    """Return (qualifies: bool, reason: str)."""
                    soid  = _safe_int(row.get(soid_col))
                    wo_id = _safe_int(row.get(so_col))
                    if soid is None or wo_id not in valid_wo_ids:
                        return False, ""
                    db_row = db_shipment.get(soid)  # None if SOID not yet in DB
                    reasons = []
                    for excel_col, db_col in _excel_to_db.items():
                        db_val    = db_row[db_col] if db_row is not None else None
                        excel_val = row.get(excel_col)
                        if db_val is None and _has_value(excel_val):
                            reasons.append(f"New {excel_col}")
                    if reasons:
                        return True, "; ".join(reasons)
                    return False, ""

                if soid_col in df.columns and so_col in df.columns:
                    _ship_quals = df.apply(_qualify_shipment, axis=1)
                    _ship_mask  = _ship_quals.apply(lambda x: x[0])
                    new_df      = df[_ship_mask].copy()
                    new_df[_ship_reason_col] = _ship_quals[_ship_mask].apply(lambda x: x[1])
                else:
                    new_df = pd.DataFrame()

                # ── Previous-month SOIDs with incomplete shipment data ─────────
                # Uses the cached incomplete list (built from the latest-known anchor
                # month).  Cross-references against the current Excel to split into:
                #   A. filled_by_excel  — cached incomplete SOIDs present in this
                #      Excel file with a non-empty value for the missing field(s)
                #      → will be fixed by this upsert.
                #   B. still_incomplete — cached incomplete SOIDs NOT covered by
                #      this Excel (or still missing after it).
                from datetime import datetime as _dt_ship
                import math as _math_ship

                _pickup_col = "Ship PickUp Time"
                _incomplete_soid_cols = [
                    "Missing Fields",
                    "SOID", "SO (Work Order ID)", "Ship PN",
                    "wo_product_status", "ship_pickup_time",
                    "AWB", "Ship POU POD Time",
                ]

                # Step 1 — derive dominant month from this Excel's pickup dates
                _excel_month: str | None = None   # "YYYY-MM"
                if _pickup_col in df.columns:
                    _months: dict[str, int] = {}
                    for _v in df[_pickup_col]:
                        if not _has_value(_v):
                            continue
                        try:
                            _iso = str(pd.to_datetime(_v).date())[:7]
                            _months[_iso] = _months.get(_iso, 0) + 1
                        except Exception:
                            pass
                    if _months:
                        _excel_month = max(_months, key=lambda k: _months[k])

                # Step 2 — load the cached incomplete list (anchor = latest known month)
                from app.services.upload.meta_cache import read_incomplete_prev_shipments as _read_inc
                _cached_incomplete = _read_inc(meta_folder)

                # Step 3 — build a lookup of what this Excel provides, keyed by SOID
                #   excel_soid_vals[soid] = {"awb": val_or_None, "ship_pou_pod_time": val_or_None}
                _excel_soid_vals: dict[int, dict] = {}
                if soid_col in df.columns:
                    for _, _er in df.iterrows():
                        _esoid = _safe_int(_er.get(soid_col))
                        if _esoid is None:
                            continue
                        _excel_soid_vals[_esoid] = {
                            "awb":               _er.get("AWB"),
                            "ship_pou_pod_time":  _er.get("Ship POU POD Time"),
                            "ship_pn":           _er.get("Ship PN"),
                        }

                # Step 4 — split cached list into filled vs still-incomplete
                _filled_by_excel:   list[dict] = []
                _still_incomplete:  list[dict] = []

                for _ci in _cached_incomplete:
                    _csoid = _safe_int(_ci.get("SOID"))
                    if _csoid is None:
                        _still_incomplete.append(_ci)
                        continue
                    _ex = _excel_soid_vals.get(_csoid)
                    if _ex is None:
                        # SOID not in this Excel at all
                        _still_incomplete.append(_ci)
                        continue
                    # Check whether Excel fills each missing field
                    _fills_awb = (
                        "AWB" in _ci.get("Missing Fields", "")
                        and _has_value(_ex.get("awb"))
                    )
                    _fills_pod = (
                        "Ship POU POD Time" in _ci.get("Missing Fields", "")
                        and _has_value(_ex.get("ship_pou_pod_time"))
                    )
                    _needs_awb = "AWB" in _ci.get("Missing Fields", "")
                    _needs_pod = "Ship POU POD Time" in _ci.get("Missing Fields", "")

                    if (_needs_awb and not _fills_awb) or (_needs_pod and not _fills_pod):
                        # Excel doesn't fully fill this row — stays incomplete
                        _still_incomplete.append(_ci)
                    else:
                        # All missing fields will be filled by this Excel
                        _filled = dict(_ci)
                        # Show what Excel will write
                        if _fills_awb:
                            _filled["AWB"] = str(_ex["awb"])
                        if _fills_pod:
                            _filled["Ship POU POD Time"] = str(_ex["ship_pou_pod_time"])
                        _filled_by_excel.append(_filled)

                # _incomplete_soids shown in modal = still-incomplete after this upsert
                _incomplete_soids: list[dict] = _still_incomplete

            elif category_key == "PARTONHOLD":
                # Impacted rows = Excel rows where:
                #   1. SOID exists in wo_product_detail AND
                #   2. wo_product_status = 'On Hold - Part Hold' AND
                #   3. SO ETA is non-empty AND
                #      (DB eta_parthold_backlog IS NULL OR Excel SO ETA is strictly newer)
                import math as _math

                def _has_val_ph(v) -> bool:
                    if v is None:
                        return False
                    if isinstance(v, float) and _math.isnan(v):
                        return False
                    s = str(v).strip()
                    return s not in ("", "nan", "nat", "none", "null", "NaT")

                # DB state: soid → eta_parthold_backlog for On Hold - Part Hold rows only
                db_partonhold = {
                    r[0]: r[1]  # soid → eta_parthold_backlog
                    for r in db_conn.execute(
                        "SELECT soid, eta_parthold_backlog FROM wo_product_detail "
                        "WHERE LOWER(COALESCE(wo_product_status, '')) = 'on hold - part hold'"
                    ).fetchall()
                }

                soid_col = "SOID"
                eta_col  = "SO ETA"
                date_col = eta_col
                preview_cols = [
                    soid_col, "Service Order ID", "Part Number", "PN Desc",
                    "ETA", eta_col, "Status", "Category",
                ]

                def _is_impacted_partonhold(row):
                    soid = _safe_int(row.get(soid_col))
                    if soid is None or soid not in db_partonhold:
                        return False
                    eta_val = row.get(eta_col)
                    if not _has_val_ph(eta_val):
                        return False
                    eta_iso = str(eta_val).strip()[:10]
                    db_eta = db_partonhold[soid]
                    if db_eta is None:
                        return True
                    # Overwrite only if Excel SO ETA is strictly newer
                    try:
                        from datetime import datetime as _dt2
                        db_dt  = _dt2.fromisoformat(db_eta[:10])
                        eta_dt = _dt2.fromisoformat(eta_iso)
                        return eta_dt > db_dt
                    except (ValueError, TypeError):
                        return False

                new_df = df[df.apply(_is_impacted_partonhold, axis=1)].copy() \
                    if (soid_col in df.columns and eta_col in df.columns) \
                    else pd.DataFrame()

            elif category_key == "GTAAP":
                # Impacted rows = Excel rows where:
                #   1. SOID exists in wo_product_detail, AND
                #   2. DB dc_number IS NULL or '0' (eligible to be filled), AND
                #   3. Excel DC# is non-null  (pass 1 — real value)
                #      OR return_flag is N/No  (pass 2 — sentinel '0')
                import math as _math

                db_dc = {
                    r[0]: r[1]  # soid → dc_number (None when empty)
                    for r in db_conn.execute(
                        "SELECT soid, dc_number FROM wo_product_detail"
                    ).fetchall()
                }

                soid_col       = "SOID"
                dc_col         = "DC#"
                rf_col         = "Return Flag"
                dc_insert_col  = "DC# (will be inserted)"
                date_col       = "Tanggal Pengiriman Suku Cadang"
                preview_cols = [
                    soid_col, "WO#", "Status",
                    dc_insert_col,
                    rf_col,
                    "Nomor Suku Cadang", "Deskripsi Suku Cadang",
                    "Nama Penyedia Layanan", date_col,
                ]

                def _has_dc_val(v) -> bool:
                    if v is None:
                        return False
                    if isinstance(v, float) and _math.isnan(v):
                        return False
                    s = str(v).strip()
                    return s not in ("", "nan", "nat", "none", "null", "NaT")

                def _dc_eligible(current) -> bool:
                    """DB value is eligible to be overwritten."""
                    return current is None or str(current).strip() == "0"

                def _dc_to_str(v):
                    """Normalise whole-number floats: 17731.0 → '17731'."""
                    if not _has_dc_val(v):
                        return ""
                    try:
                        f = float(v)
                        if f == int(f):
                            return str(int(f))
                    except (ValueError, TypeError):
                        pass
                    return str(v).strip()

                _no_return = {"n", "no"}

                def _is_new_dc(row):
                    soid = _safe_int(row.get(soid_col))
                    if soid is None or soid not in db_dc:
                        return False
                    # Only rows whose DB value is eligible (NULL or '0')
                    if not _dc_eligible(db_dc[soid]):
                        return False
                    dc_val = row.get(dc_col)
                    if _has_dc_val(dc_val):
                        return True   # pass 1: real DC# to write
                    # pass 2: no real DC# but return_flag is N → will write '0'
                    return_flag = str(row.get(rf_col) or "").strip().lower()
                    return return_flag in _no_return

                def _preview_dc(row):
                    """Value that will actually be written — real DC# or '0'."""
                    dc_val = row.get(dc_col)
                    if _has_dc_val(dc_val):
                        return _dc_to_str(dc_val)
                    return_flag = str(row.get(rf_col) or "").strip().lower()
                    if return_flag in _no_return:
                        return "0"
                    return ""

                if soid_col in df.columns and dc_col in df.columns:
                    new_df = df[df.apply(_is_new_dc, axis=1)].copy()
                    new_df[dc_insert_col] = new_df.apply(_preview_dc, axis=1)
                else:
                    new_df = pd.DataFrame()

            else:
                return jsonify({"ok": False, "error": "Category has no DB preview support."})

        finally:
            db_conn.close()

        impacted_count = len(new_df)

        # WOID and SOID: sort by Modified On ascending (oldest first).
        # All other categories: sort by date_col descending (newest first).
        _sort_asc = (category_key in ("WOID", "SOID"))

        def _all_rows_sorted(frame):
            if frame.empty or date_col not in frame.columns:
                return []
            tmp = frame.copy()
            tmp["_sort_date"] = pd.to_datetime(tmp[date_col], errors="coerce")
            tmp = tmp.sort_values("_sort_date", ascending=_sort_asc, na_position="last")
            tmp = tmp.drop(columns=["_sort_date"])
            cols = [c for c in preview_cols if c in tmp.columns]
            rows_out = []
            for _, r in tmp[cols].iterrows():
                rows_out.append({
                    k: ("" if (v is None or (isinstance(v, float) and __import__("math").isnan(v)))
                        else str(v))
                    for k, v in r.items()
                })
            return rows_out

        # Use new_df for WOID, GTAAP, SOID, and SHIPMENT because they carry a synthetic
        # "Reason" / "Upsert Reason" / "DC# (will be inserted)" column not in the raw df.
        _preview_col_source = new_df if (not new_df.empty and category_key in ("WOID", "GTAAP", "SOID", "SHIPMENT")) else df
        _resp = {
            "ok":               True,
            "category_key":     category_key,
            "filename":         target_file,
            "impacted_count":   impacted_count,
            "total_excel_rows": len(df),
            "date_col":         date_col,
            "preview_cols":     [c for c in preview_cols if c in _preview_col_source.columns],
            "all_rows":         _all_rows_sorted(new_df),
        }
        if category_key == "WOID":
            _resp["active_wo_not_in_excel"] = active_wo_not_in_excel
            _resp["active_wo_cols"]         = active_wo_cols
        if category_key == "SOID":
            _resp["wo_product_mismatch"]      = _soid_mismatch_rows
            _resp["wo_product_mismatch_cols"] = [
                "Reason", "Created On", "Work Order ID", "Line Order",
                "Product", "Description", "WO Product Status",
            ]
        if category_key == "SHIPMENT":
            _resp["incomplete_prev_soids"]      = _incomplete_soids
            _resp["incomplete_prev_soid_cols"]  = _incomplete_soid_cols
            _resp["excel_month"]                = _excel_month or ""
            _resp["filled_by_excel"]            = _filled_by_excel
            _resp["filled_by_excel_cols"]       = _incomplete_soid_cols
        return jsonify(_resp)

    except Exception:
        current_app.logger.error(
            "upsert_preview failed for %s:\n%s", category_key, _tb.format_exc()
        )
        return jsonify({"ok": False, "error": "Preview failed. Check server logs."})


@admin_bp.route("/admin/data-import/upsert/<category_key>", methods=["POST"])
def data_import_upsert(category_key: str):
    """Manual upsert: push the already-uploaded file for *category_key* into the DB."""
    import traceback as _tb
    category_key = category_key.upper()
    if category_key not in FILE_CATEGORY_CONFIGS:
        flash(f'Unknown category "{category_key}".', "danger")
        return redirect(url_for("admin.data_import"))

    upload_folder = current_app.config["EXCEL_UPLOAD_FOLDER"]
    meta_folder   = current_app.config["UPLOAD_META_FOLDER"]

    # Find the uploaded file for this category
    cat_name = FILE_CATEGORY_CONFIGS[category_key]["file_category"]
    target_file = None
    for fname in os.listdir(upload_folder):
        from app.services.upload.excel import allowed_excel as _allowed
        if not _allowed(fname):
            continue
        m = read_meta(meta_folder, fname)
        if m and m.get("file_category") == cat_name:
            target_file = fname
            break

    if not target_file:
        flash(f'No uploaded file found for category "{cat_name}".', "warning")
        return redirect(url_for("admin.data_import"))

    filepath = os.path.join(upload_folder, target_file)
    try:
        db_path = current_app.config["DATABASE_PATH"]
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        try:
            n_rows = dispatch_upsert(category_key, filepath, db_conn)
        finally:
            db_conn.close()
        mark_upserted(meta_folder, target_file)
        # ── SOID: rebuild the WO-product mismatch cache ───────────────────────
        # Only written here (on confirmed upsert), never during preview-only.
        # Cancel / refresh without confirming leaves the previous cache intact.
        if category_key == "SOID":
            try:
                import io as _io_soid
                import pandas as _pd_soid
                from app.services.upload.upload_verification import (
                    verify_uploaded_file as _vuf_soid,
                )
                from app.services.database.seed import _safe_int as _si_soid
                _vr_soid = _vuf_soid(filepath)
                _sn_soid = _vr_soid.get("sheet_name", "")
                with open(filepath, "rb") as _fh_soid:
                    _fb_soid = _io_soid.BytesIO(_fh_soid.read())
                _df_soid = (
                    _pd_soid.read_excel(_fb_soid, sheet_name=_sn_soid)
                    if _sn_soid else _pd_soid.read_excel(_fb_soid)
                )
                _db_soid = sqlite3.connect(db_path)
                _db_soid.row_factory = sqlite3.Row
                try:
                    _valid_ids_soid = {
                        r[0] for r in _db_soid.execute(
                            "SELECT work_order_id FROM wo_summary"
                        ).fetchall()
                    }
                    _detail_ids_soid = {
                        r[0] for r in _db_soid.execute(
                            "SELECT work_order_id FROM wo_details"
                        ).fetchall()
                    }
                finally:
                    _db_soid.close()
                _wo_col_soid  = "Work Order"
                _ln_col_soid  = "Line Order"
                _mismatch_soid: list[dict] = []
                _seen_soid: set[int] = set()
                if _wo_col_soid in _df_soid.columns:
                    for _, _mr_s in _df_soid.iterrows():
                        _mwo_s = _si_soid(_mr_s.get(_wo_col_soid))
                        if _mwo_s is None:
                            continue
                        if _mwo_s not in _valid_ids_soid and _mwo_s not in _detail_ids_soid:
                            if _mwo_s not in _seen_soid:
                                _seen_soid.add(_mwo_s)
                                _cr_raw = _mr_s.get("Created On")
                                try:
                                    _cr_str = str(_pd_soid.to_datetime(_cr_raw).date()) \
                                        if _cr_raw is not None and str(_cr_raw).strip() not in ("", "nan", "NaT") \
                                        else ""
                                except Exception:
                                    _cr_str = str(_cr_raw or "").strip()[:10]
                                _mismatch_soid.append({
                                    "Reason":            "WO Not Found",
                                    "Created On":        _cr_str,
                                    "Work Order ID":     str(_mwo_s),
                                    "Line Order":        str(_mr_s.get(_ln_col_soid) or ""),
                                    "Product":           str(_mr_s.get("Product") or ""),
                                    "Description":       str(_mr_s.get("Description") or ""),
                                    "WO Product Status": str(_mr_s.get("Work Order Product Status") or ""),
                                })
                write_wo_product_mismatch(meta_folder, _mismatch_soid)
            except Exception:
                current_app.logger.warning(
                    "write_wo_product_mismatch failed after upsert:\n" + _tb.format_exc()
                )
        # Rebuild the active-WO cache after a WOID upsert.
        # Pass the WO IDs from the Excel file so the cache only stores WOs
        # that were genuinely absent from this upload (matching the modal view).
        if category_key == "SHIPMENT":
            # Rebuild the incomplete-prev-shipments cache after a SHIPMENT upsert.
            # Derive the current Excel month from the uploaded file's pickup dates.
            try:
                import io as _io3
                import pandas as _pd3
                from app.services.upload.upload_verification import (
                    verify_uploaded_file as _vuf3,
                )
                _vr3 = _vuf3(filepath)
                _sn3 = _vr3.get("sheet_name", "")
                with open(filepath, "rb") as _fh3:
                    _fb3 = _io3.BytesIO(_fh3.read())
                _df3 = (
                    _pd3.read_excel(_fb3, sheet_name=_sn3)
                    if _sn3 else _pd3.read_excel(_fb3)
                )
                _months3: dict[str, int] = {}
                for _v3 in _df3.get("Ship PickUp Time", _pd3.Series(dtype=object)):
                    try:
                        _iso3 = str(_pd3.to_datetime(_v3).date())[:7]
                        _months3[_iso3] = _months3.get(_iso3, 0) + 1
                    except Exception:
                        pass
                # Use the MAXIMUM month across all months in the Excel so
                # uploading an older monthly report never regresses the anchor.
                _excel_month3 = max(_months3.keys()) if _months3 else ""
                if _excel_month3:
                    # Also compare against the existing cached anchor so we
                    # never move the anchor backwards.
                    from app.services.upload.meta_cache import (
                        read_incomplete_prev_shipments as _read_inc3,
                    )
                    _cur_cache3 = _read_inc3(meta_folder)
                    if _cur_cache3:
                        # Infer current anchor from the highest ship_pickup_time in cache
                        _cached_months3 = [
                            r.get("ship_pickup_time", "")[:7]
                            for r in _cur_cache3
                            if r.get("ship_pickup_time", "")[:7]
                        ]
                        if _cached_months3:
                            _anchor_from_cache = max(_cached_months3)
                            # advance anchor by one month so cached rows are included
                            import calendar as _cal3
                            _y3, _m3 = int(_anchor_from_cache[:4]), int(_anchor_from_cache[5:7])
                            _m3 += 1
                            if _m3 > 12:
                                _m3, _y3 = 1, _y3 + 1
                            _cache_anchor_next = f"{_y3:04d}-{_m3:02d}"
                            _excel_month3 = max(_excel_month3, _cache_anchor_next)
                    rebuild_incomplete_prev_shipments(meta_folder, db_path, _excel_month3)
            except Exception:
                current_app.logger.warning(
                    "rebuild_incomplete_prev_shipments failed after upsert:\n" + _tb.format_exc()
                )
        if category_key == "WOID":
            try:
                import io as _io2
                import pandas as _pd2
                from app.services.upload.upload_verification import (
                    verify_uploaded_file as _vuf,
                )
                _vr = _vuf(filepath)
                _sn = _vr.get("sheet_name", "")
                with open(filepath, "rb") as _fh2:
                    _fb2 = _io2.BytesIO(_fh2.read())
                _df2 = (
                    _pd2.read_excel(_fb2, sheet_name=_sn)
                    if _sn else _pd2.read_excel(_fb2)
                )
                _excel_ids: set[int] = {
                    int(float(str(v)))
                    for v in _df2.get("Work Order ID", _pd2.Series(dtype=object))
                    if v is not None and str(v).strip() not in ("", "nan", "NaN")
                }
                rebuild_active_open_wos(meta_folder, db_path, _excel_ids)
            except Exception:
                current_app.logger.warning(
                    "rebuild_active_open_wos failed after upsert:\n" + _tb.format_exc()
                )
        flash(
            f'"{target_file}" upserted successfully — {n_rows} row{"s" if n_rows != 1 else ""} processed.',
            "success",
        )
    except Exception:
        current_app.logger.error("manual upsert failed for %s:\n%s", target_file, _tb.format_exc())
        flash(f'Upsert failed for "{target_file}". Check server logs.', "danger")

    return redirect(url_for("admin.data_import"))


@admin_bp.route("/admin/data-import/delete/<filename>", methods=["POST"])
def data_import_delete(filename):
    safe = secure_filename(filename)
    folder      = current_app.config["EXCEL_UPLOAD_FOLDER"]
    meta_folder = current_app.config["UPLOAD_META_FOLDER"]
    path = os.path.join(folder, safe)
    if os.path.isfile(path):
        os.remove(path)
        delete_meta(meta_folder, safe)
        flash(f'File "{safe}" deleted.', "success")
    else:
        flash("File not found.", "danger")
    return redirect(url_for("admin.data_import"))


@admin_bp.route("/admin/data-import/reset", methods=["POST"])
def data_import_reset():
    import traceback as _tb, sys
    try:
        folder      = current_app.config["EXCEL_UPLOAD_FOLDER"]
        meta_folder = current_app.config["UPLOAD_META_FOLDER"]
        deleted = 0
        if os.path.isdir(folder):
            for fname in list(os.listdir(folder)):
                if not allowed_excel(fname):
                    continue
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    delete_meta(meta_folder, fname)
                    deleted += 1
        flash(f"Reset complete — {deleted} file{'s' if deleted != 1 else ''} deleted.", "success")
    except Exception:
        tb = _tb.format_exc()
        print("=== data_import_reset ERROR ===", file=sys.stderr)
        print(tb, file=sys.stderr)
        flash(f"Reset failed: {tb.splitlines()[-1]}", "danger")
    return redirect(url_for("admin.data_import"))


# ── Validation Center ────────────────────────────────────────────────────────

@admin_bp.route("/admin/validation", methods=["GET"])
def validation():
    return render_template("admin/validation_center.html",
                           portal="admin", active_page="validation")


# ── User & ASP Management ────────────────────────────────────────────────────

@admin_bp.route("/admin/users", methods=["GET"])
def users():
    return render_template("admin/user_management.html",
                           portal="admin", active_page="user_mgmt", active_group="user_mgmt")


# Kota → granular Java region mapping.
# Anything on Java that doesn't match stays as "Jawa".
_JABODETABEK = {
    "jakarta pusat", "jakarta barat", "jakarta selatan", "jakarta utara",
    "jakarta timur", "bogor", "depok", "tangerang", "bekasi", "banten",
    "karawang", "cikarang",
}
_JAWA_BARAT = {
    "bandung", "cirebon", "tasikmalaya", "garut", "sukabumi",
    "cianjur", "purwakarta", "subang", "indramayu",
}
_JAWA_TENGAH = {
    "semarang", "kudus", "pati", "tegal", "surakarta", "solo",
    "magelang", "purwokerto", "salatiga", "klaten", "wonosobo",
    "banyumas", "cilacap",
}
_JAWA_TIMUR = {
    "surabaya", "malang", "kediri", "jember", "madiun", "mojokerto",
    "pasuruan", "probolinggo", "blitar", "banyuwangi",
}
_DIY = {"yogyakarta", "sleman", "bantul", "gunung kidul", "kulonprogo"}


def _resolve_region(kota: str | None, island: str | None) -> str:
    """Return a granular region label for a given kota / island pair."""
    if kota:
        k = kota.strip().lower()
        if k in _JABODETABEK:
            return "Jabodetabek"
        if k in _JAWA_BARAT:
            return "Jawa Barat"
        if k in _JAWA_TENGAH:
            return "Jawa Tengah"
        if k in _JAWA_TIMUR:
            return "Jawa Timur"
        if k in _DIY:
            return "DIY"
    # Fall back to the island column (strip + title-case)
    if island and island.strip():
        raw = island.strip()
        # Normalise casing inconsistencies (e.g. "jawa" → "Jawa")
        return raw[0].upper() + raw[1:]
    return "—"


@admin_bp.route("/admin/users/asp-directory", methods=["GET"])
def asp_directory():
    db_path = current_app.config["DATABASE_PATH"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Refresh wo_count for all ASPs in one UPDATE pass
    conn.execute(
        """
        UPDATE asp_details
        SET wo_count = (
            SELECT COUNT(*)
            FROM wo_details
            WHERE wo_details.labor_vendor_related = asp_details.labor_vendor_related
        )
        """
    )
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM asp_details ORDER BY id"
    ).fetchall()
    conn.close()

    # Attach computed region to each row as a plain dict so the template
    # can access asp.region without modifying the DB schema.
    asps = []
    for r in rows:
        d = dict(r)
        d["region"] = _resolve_region(d.get("kota"), d.get("island"))
        asps.append(d)

    regions = sorted({a["region"] for a in asps if a["region"] != "—"})

    return render_template(
        "admin/user_management/asp_directory.html",
        asps=asps,
        regions=regions,
        portal="admin",
        active_page="asp_directory",
        active_group="user_mgmt",
    )


@admin_bp.route("/admin/users/asp-directory/<int:asp_id>/edit", methods=["POST"])
def asp_directory_edit(asp_id):
    fields = [
        "username", "password", "vendor_code", "service_provider",
        "parent_group", "labor_vendor_related", "customer_partner",
        "store_name", "kota", "address", "lat_long", "link_map",
        "phone_number", "island", "working_hours", "operational_status",
        "future_status", "operation_support", "office_type",
    ]
    values = {f: request.form.get(f, "").strip() or None for f in fields}
    set_clause = ", ".join(f"{f} = ?" for f in fields)
    params = [values[f] for f in fields] + [asp_id]

    db_path = current_app.config["DATABASE_PATH"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE asp_details SET {set_clause} WHERE id = ?", params
        )
        conn.commit()
        flash(f"ASP record #{asp_id} updated successfully.", "success")
    finally:
        conn.close()
    return redirect(url_for("admin.asp_directory"))


@admin_bp.route("/admin/users/asp-directory/create", methods=["POST"])
def asp_directory_create():
    fields = [
        "username", "password", "vendor_code", "service_provider",
        "parent_group", "labor_vendor_related", "customer_partner",
        "store_name", "kota", "address", "lat_long", "link_map",
        "phone_number", "island", "working_hours", "operational_status",
        "future_status", "operation_support", "office_type",
    ]
    values = {f: request.form.get(f, "").strip() or None for f in fields}

    # Require username and labor_vendor_related
    if not values.get("username") or not values.get("labor_vendor_related"):
        flash("Username and ASP ID are required to create a new ASP.", "danger")
        return redirect(url_for("admin.asp_directory"))

    cols   = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    params = [values[f] for f in fields]

    db_path = current_app.config["DATABASE_PATH"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO asp_details ({cols}) VALUES ({placeholders})", params
        )
        conn.commit()
        flash(f"ASP \"{values['username']}\" created successfully.", "success")
    except sqlite3.IntegrityError:
        flash(f"Username \"{values['username']}\" already exists in asp_details.", "danger")
    finally:
        conn.close()
    return redirect(url_for("admin.asp_directory"))


# ── ASP Users (admin read) ───────────────────────────────────────────────────

@admin_bp.route("/admin/users/asp-directory/<asp_username>/users", methods=["GET"])
def asp_directory_users(asp_username):
    """Return the asp_users list for a given ASP as JSON (admin use)."""
    from app.services.database.db import get_db
    db = get_db()
    rows = db.execute(
        """SELECT id, full_name, email, phone_number, is_active, created_at
           FROM asp_users
           WHERE asp_username = ?
           ORDER BY id""",
        (asp_username,)
    ).fetchall()
    return jsonify({"ok": True, "users": [dict(r) for r in rows]})


# ── ASP Password Change Requests ─────────────────────────────────────────────

@admin_bp.route("/admin/users/pw-change-requests", methods=["GET"])
def pw_change_requests():
    """List the auto-approved ASP password change history."""
    from app.services.database.db import get_db
    db = get_db()
    history_rows = db.execute(
        """SELECT r.id, r.asp_username, r.requested_at, r.status,
                  r.new_password, r.reviewed_by, r.reviewed_at,
                  d.service_provider
           FROM asp_pw_change_requests r
           LEFT JOIN asp_details d ON d.username = r.asp_username
           ORDER BY r.requested_at DESC"""
    ).fetchall()
    return render_template(
        "admin/user_management/pw_change_requests.html",
        history=[dict(r) for r in history_rows],
        portal="admin",
        active_page="pw_change_requests",
        active_group="user_mgmt",
    )


@admin_bp.route("/admin/users/pw-change-requests/<int:req_id>/approve", methods=["POST"])
def pw_change_request_approve(req_id):
    """Approve a password change request: apply the ASP's requested password."""
    from app.services.database.db import get_db
    from flask import session as _session
    db = get_db()
    row = db.execute(
        "SELECT asp_username, new_password FROM asp_pw_change_requests "
        "WHERE id=? AND status='pending'",
        (req_id,)
    ).fetchone()
    if not row:
        flash("Request not found or already reviewed.", "danger")
        return redirect(url_for("admin.pw_change_requests"))
    asp_username = row["asp_username"]
    new_password = row["new_password"]
    if not new_password:
        flash("No password was submitted with this request.", "danger")
        return redirect(url_for("admin.pw_change_requests"))
    reviewed_by = _session.get("username", "admin")
    db.execute(
        "UPDATE asp_details SET password=? WHERE username=?",
        (new_password, asp_username)
    )
    db.execute(
        "UPDATE asp_pw_change_requests "
        "SET status='approved', reviewed_by=?, reviewed_at=datetime('now') "
        "WHERE id=?",
        (reviewed_by, req_id)
    )
    db.commit()
    flash(f"Password for {asp_username} approved and applied successfully.", "success")
    return redirect(url_for("admin.pw_change_requests"))


@admin_bp.route("/admin/users/pw-change-requests/<int:req_id>/deny", methods=["POST"])
def pw_change_request_deny(req_id):
    """Deny a password change request."""
    from app.services.database.db import get_db
    from flask import session as _session
    db = get_db()
    reviewed_by = _session.get("username", "admin")
    db.execute(
        "UPDATE asp_pw_change_requests "
        "SET status='denied', reviewed_by=?, reviewed_at=datetime('now') "
        "WHERE id=? AND status='pending'",
        (reviewed_by, req_id)
    )
    db.commit()
    flash("Request denied.", "warning")
    return redirect(url_for("admin.pw_change_requests"))


# ── System Archive ───────────────────────────────────────────────────────────

@admin_bp.route("/admin/archive", methods=["GET"])
def archive():
    files = list_excel_uploads()
    for f in files:
        f["modified_fmt"] = datetime.fromtimestamp(f["modified"]).strftime("%Y-%m-%d %H:%M")
    return render_template("admin/system_archive.html",
                           masterfiles=[],
                           uploaded_files=files,
                           portal="admin", active_page="archive")


@admin_bp.route("/admin/archive/download/masterfile/<filename>", methods=["GET"])
def archive_download(filename):
    from werkzeug.utils import secure_filename
    safe     = secure_filename(filename)
    filepath = os.path.join(current_app.config["EXCELS_DIR"], safe)
    if not os.path.isfile(filepath):
        flash("File not found.", "danger")
        return redirect(url_for("admin.archive"))
    return send_file(filepath, as_attachment=True, download_name=safe,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── Superadmin Users (admin_users table, role = 'superadmin') ────────────────

@admin_bp.route("/admin/api/superadmin-users", methods=["GET"])
def api_superadmin_users_list():
    """Return all superadmin accounts (role = 'superadmin') for the profile page."""
    from flask import session as _session
    if _session.get("role") != "superadmin":
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    from app.services.database.db import get_db
    db = get_db()
    rows = db.execute(
        "SELECT id, username, full_name, email, is_active, created_at "
        "FROM admin_users WHERE role = 'superadmin' ORDER BY id"
    ).fetchall()
    return jsonify({"ok": True, "users": [dict(r) for r in rows]})


@admin_bp.route("/admin/api/superadmin-users", methods=["POST"])
def api_superadmin_users_create():
    """Create a new superadmin account in admin_users."""
    from flask import session as _session
    if _session.get("role") != "superadmin":
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    from app.services.database.db import get_db
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    fullname = (data.get("full_name") or "").strip()
    email    = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    if not username:
        return jsonify({"ok": False, "error": "Username is required.", "field": "username"})
    if not password or len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters.", "field": "password"})

    db = get_db()
    existing = db.execute(
        "SELECT id FROM admin_users WHERE LOWER(username) = LOWER(?)", (username,)
    ).fetchone()
    if existing:
        return jsonify({"ok": False, "error": "Username already exists.", "field": "username"})

    db.execute(
        "INSERT INTO admin_users (username, password, full_name, email, role, is_active) "
        "VALUES (?, ?, ?, ?, 'superadmin', 1)",
        (username, password, fullname or None, email or None),
    )
    db.commit()
    row = db.execute(
        "SELECT id, username, full_name, email, is_active, created_at "
        "FROM admin_users WHERE LOWER(username) = LOWER(?)", (username,)
    ).fetchone()
    return jsonify({"ok": True, "user": dict(row)})


@admin_bp.route("/admin/api/superadmin-users/<int:uid>", methods=["PUT"])
def api_superadmin_users_update(uid):
    """Update username / full_name / email / password for a superadmin account."""
    from flask import session as _session
    if _session.get("role") != "superadmin":
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    # Prevent a superadmin from editing themselves via this endpoint (use profile page instead)
    if _session.get("user_id") == uid:
        return jsonify({"ok": False, "error": "Cannot edit your own account here."})
    from app.services.database.db import get_db
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    fullname = (data.get("full_name") or "").strip()
    email    = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    if not username:
        return jsonify({"ok": False, "error": "Username is required.", "field": "username"})

    db = get_db()
    # Confirm target exists and is superadmin
    target = db.execute(
        "SELECT id FROM admin_users WHERE id = ? AND role = 'superadmin'", (uid,)
    ).fetchone()
    if not target:
        return jsonify({"ok": False, "error": "User not found."})

    # Uniqueness check (excluding self)
    clash = db.execute(
        "SELECT id FROM admin_users WHERE LOWER(username) = LOWER(?) AND id != ?",
        (username, uid)
    ).fetchone()
    if clash:
        return jsonify({"ok": False, "error": "Username already taken.", "field": "username"})

    if password:
        if len(password) < 8:
            return jsonify({"ok": False, "error": "Password must be at least 8 characters.", "field": "password"})
        db.execute(
            "UPDATE admin_users SET username=?, full_name=?, email=?, password=? WHERE id=?",
            (username, fullname or None, email or None, password, uid),
        )
    else:
        db.execute(
            "UPDATE admin_users SET username=?, full_name=?, email=? WHERE id=?",
            (username, fullname or None, email or None, uid),
        )
    db.commit()
    row = db.execute(
        "SELECT id, username, full_name, email, is_active, created_at FROM admin_users WHERE id=?", (uid,)
    ).fetchone()
    return jsonify({"ok": True, "user": dict(row)})


@admin_bp.route("/admin/api/superadmin-users/<int:uid>/status", methods=["PATCH"])
def api_superadmin_users_status(uid):
    """Toggle is_active for a superadmin account."""
    from flask import session as _session
    if _session.get("role") != "superadmin":
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if _session.get("user_id") == uid:
        return jsonify({"ok": False, "error": "Cannot change your own account status."})
    from app.services.database.db import get_db
    data      = request.get_json(silent=True) or {}
    is_active = 1 if data.get("is_active") else 0
    db = get_db()
    target = db.execute(
        "SELECT id FROM admin_users WHERE id = ? AND role = 'superadmin'", (uid,)
    ).fetchone()
    if not target:
        return jsonify({"ok": False, "error": "User not found."})
    db.execute("UPDATE admin_users SET is_active=? WHERE id=?", (is_active, uid))
    db.commit()
    return jsonify({"ok": True, "is_active": is_active})


@admin_bp.route("/admin/api/superadmin-users/<int:uid>", methods=["DELETE"])
def api_superadmin_users_delete(uid):
    """Permanently delete a superadmin account."""
    from flask import session as _session
    if _session.get("role") != "superadmin":
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    if _session.get("user_id") == uid:
        return jsonify({"ok": False, "error": "Cannot delete your own account."})
    from app.services.database.db import get_db
    db = get_db()
    target = db.execute(
        "SELECT id FROM admin_users WHERE id = ? AND role = 'superadmin'", (uid,)
    ).fetchone()
    if not target:
        return jsonify({"ok": False, "error": "User not found."})
    db.execute("DELETE FROM admin_users WHERE id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


# ── Escalation Center (Monday.com Sync Manager) ──────────────────────────────
# Background sync state — module-level so it persists across requests

SYNC_INTERVAL_SEC = 30 * 60   # 30 minutes
SYNC_TZ_OFFSET    = 7 * 3600  # WIB = UTC+7
SYNC_HOUR_START   = 6         # 06:00 local
SYNC_HOUR_END     = 20        # 20:00 local (exclusive)

_sync_thread: threading.Thread | None = None
_sync_stop        = threading.Event()
_log_queue: _queue.Queue = _queue.Queue(maxsize=2000)
_sync_lock        = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop   = threading.Event()
_next_run_at: float | None = None      # Unix ts of next scheduled sync run
_window_opens_at: float | None = None  # Unix ts of next active-window open (when idle/off-hours)


def _local_now():
    """Current time as a datetime in WIB (UTC+7), timezone-naive."""
    from datetime import datetime, timezone, timedelta
    tz_wib = timezone(timedelta(seconds=SYNC_TZ_OFFSET))
    return datetime.now(tz_wib).replace(tzinfo=None)


def _is_active_window(dt=None):
    """Return True if dt (or now) is a weekday between SYNC_HOUR_START and SYNC_HOUR_END."""
    if dt is None:
        dt = _local_now()
    return dt.weekday() < 5 and SYNC_HOUR_START <= dt.hour < SYNC_HOUR_END


def _next_window_open_ts():
    """Return Unix timestamp (UTC) of when the next active window begins."""
    from datetime import datetime, timezone, timedelta
    tz_wib = timezone(timedelta(seconds=SYNC_TZ_OFFSET))
    now_local = _local_now()
    # Start from tomorrow if today's window already passed or it's a weekend
    candidate = now_local.replace(hour=SYNC_HOUR_START, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        # Today's window start is in the past — move to next day
        candidate = candidate + timedelta(days=1)
    # Advance past weekends (Saturday=5, Sunday=6)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    # Convert back to UTC Unix timestamp
    candidate_aware = candidate.replace(tzinfo=tz_wib)
    return candidate_aware.timestamp()


_queue_formatter = logging.Formatter(datefmt="%Y-%m-%d %H:%M:%S")


class _QueueHandler(logging.Handler):
    """Logging handler that pushes records into the SSE queue."""
    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        # Only forward records that originated from the monday_sync logger
        if not record.name.startswith("monday_sync"):
            return
        try:
            _log_queue.put_nowait({
                "ts":    _queue_formatter.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "msg":   record.getMessage(),
            })
        except _queue.Full:
            # Queue full — drop oldest entry to make room for this one
            try:
                _log_queue.get_nowait()
                _log_queue.put_nowait({
                    "ts":    _queue_formatter.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                    "level": record.levelname,
                    "msg":   record.getMessage(),
                })
            except (_queue.Full, _queue.Empty):
                pass


_queue_handler = _QueueHandler()
_queue_handler.setLevel(logging.DEBUG)


def _get_monday_sync_db():
    """Open a fresh SQLite connection to files/lenovo_asp.db."""
    import os as _os, sqlite3 as _sqlite3
    project_root = _os.path.normpath(
        _os.path.join(_os.path.dirname(__file__), "..", "..")
    )
    db_path = _os.path.join(project_root, "files", "lenovo_asp.db")
    if not _os.path.isfile(db_path):
        return None
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    return conn





def _scheduler_loop(app) -> None:
    """
    Auto-sync loop — runs forever as a daemon thread.

    Guarantees:
      • Only runs during active window: Mon–Fri 06:00–20:00 WIB.
      • Outside the window it sleeps (interruptibly) until the window opens.
      • Never starts a new sync while the previous one is still running.
      • The 30-min cooldown starts ONLY after the previous sync finishes.
      • Responds to _scheduler_stop within 1 second for clean shutdown.
    """
    global _sync_thread, _next_run_at, _window_opens_at
    log = logging.getLogger("monday_sync")
    log.info("Auto-scheduler started — interval=%d min, window=%02d:00–%02d:00 WIB Mon–Fri",
             SYNC_INTERVAL_SEC // 60, SYNC_HOUR_START, SYNC_HOUR_END)

    # ── Startup delay: wait 15 minutes before first sync ────────────────────
    STARTUP_DELAY_SEC = 15 * 60
    log.info("Scheduler: startup delay — first sync in 15 min")
    _next_run_at = _time.time() + STARTUP_DELAY_SEC   # expose to status endpoint
    _scheduler_stop.wait(STARTUP_DELAY_SEC)
    _next_run_at = None
    if _scheduler_stop.is_set():
        log.info("Auto-scheduler stopped during startup delay.")
        return

    while not _scheduler_stop.is_set():

        # ── Step 0: enforce active-window gate ───────────────────────────────
        if not _is_active_window():
            opens_ts = _next_window_open_ts()
            wait_sec = max(0, opens_ts - _time.time())
            _window_opens_at = opens_ts
            _next_run_at     = None
            log.info("Scheduler: outside active window — sleeping %.0f min until window opens",
                     wait_sec / 60)
            _scheduler_stop.wait(wait_sec)
            _window_opens_at = None
            if _scheduler_stop.is_set():
                break
            continue   # re-check window at top of loop

        _window_opens_at = None   # we are inside the window

        # ── Step 1: wait for any in-progress sync (manual or previous scheduled)
        while True:
            with _sync_lock:
                running_thread = _sync_thread if (_sync_thread and _sync_thread.is_alive()) else None
            if running_thread is None:
                break
            log.info("Scheduler: sync already running, waiting for it to finish…")
            running_thread.join()

        if _scheduler_stop.is_set():
            break

        # ── Step 2: decide mode based on current DB contents
        edb = _get_monday_sync_db()
        item_count = 0
        if edb:
            try:
                item_count = edb.execute(
                    "SELECT COUNT(*) FROM technical_escalation"
                ).fetchone()[0]
            except Exception:
                pass
            finally:
                edb.close()
        mode = "incremental" if item_count > 0 else "full"

        # ── Step 3: claim the sync slot and start the thread
        with _sync_lock:
            if _sync_thread and _sync_thread.is_alive():
                continue
            _sync_stop.clear()
            t = threading.Thread(
                target=_run_sync_task,
                args=(app, mode, None),
                daemon=True,
                name="monday-sync-auto",
            )
            _sync_thread = t

        log.info("Scheduler: starting %s sync (%d items in DB)", mode, item_count)
        t.start()
        t.join()   # ← 30-min clock starts ONLY after this returns

        log.info("Scheduler: sync complete — next run in %d min", SYNC_INTERVAL_SEC // 60)

        # ── Step 4: interruptible sleep before next cycle
        # Only schedule next run if still inside the active window
        if _is_active_window():
            _next_run_at = _time.time() + SYNC_INTERVAL_SEC
            _scheduler_stop.wait(SYNC_INTERVAL_SEC)
            _next_run_at = None
        # If we've drifted outside the window, the loop will handle it on next iteration

    log.info("Auto-scheduler stopped.")


def start_sync_scheduler(app) -> None:
    """
    Start the background scheduler thread exactly once.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(app,),
        daemon=True,
        name="monday-scheduler",
    )
    _scheduler_thread.start()


def _run_sync_task(app, mode: str, board_id: str | None = None) -> None:
    """
    Background thread target: runs the Monday sync script.
    Attaches a _QueueHandler to the monday_sync logger so every log record
    is forwarded to the SSE queue.
    """
    import sys
    import os as _os

    project_root = _os.path.normpath(
        _os.path.join(_os.path.dirname(__file__), "..", "..")
    )
    scripts_dir = _os.path.join(project_root, "app", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    def _put(level: str, msg: str) -> None:
        """Put a log entry into the queue, dropping the oldest if full."""
        entry = {"ts": "", "level": level, "msg": msg}
        try:
            _log_queue.put_nowait(entry)
        except _queue.Full:
            try:
                _log_queue.get_nowait()
                _log_queue.put_nowait(entry)
            except (_queue.Full, _queue.Empty):
                pass

    try:
        import monday_sync as _ms
    except ImportError as exc:
        _put("ERROR", f"Cannot import monday_sync: {exc}")
        return

    # Wire queue handler into the sync script's logger
    _ms_logger = logging.getLogger("monday_sync")
    _ms_logger.propagate = False  # prevent leaking into root → _msd_log_queue
    _ms_logger.addHandler(_queue_handler)
    _ms_logger.setLevel(logging.DEBUG)

    xlsx_path = _os.path.join(project_root, "files", "source-db", "monday_link_map.xlsx")

    with app.app_context():
        try:
            conn  = _ms.get_db(_os.path.join(project_root, "files", "lenovo_asp.db"))
            state = _ms.load_state()
            boards = _ms.load_boards(xlsx_path)

            if board_id:
                boards = [b for b in boards if b["board_id"] == board_id]
                if not boards:
                    _put("ERROR", f"Board ID {board_id} not found in xlsx")
                    return

            if mode == "full":
                _ms.run_sync_all(conn, state, boards, force_full=True)
            elif mode == "incremental":
                _ms.run_sync_all(conn, state, boards, force_full=False)
            elif mode == "backfill":
                _ms._backfill_updates(conn)
            else:
                _put("ERROR", f"Unknown sync mode: {mode}")
                return

            _put("INFO", f"✓ Sync mode '{mode}' completed.")
        except Exception as exc:
            import traceback
            _put("ERROR", f"Sync error: {exc}")
            _put("ERROR", traceback.format_exc())
        finally:
            _ms_logger.removeHandler(_queue_handler)
            # Keep propagate=False permanently — restoring True would let Werkzeug
            # access log records leak into _msd_log_queue via the root logger.
            try:
                conn.close()
            except Exception:
                pass


def _run_msd_download_task(app, run_once: bool = False) -> None:
    import runpy
    import traceback
    from contextlib import redirect_stderr, redirect_stdout

    class _QueueWriter:
        def __init__(self, logger: logging.Logger, level: int) -> None:
            self._logger = logger
            self._level = level
            self._buffer = ""

        def write(self, value: str) -> int:
            self._buffer += value
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line)
            return len(value)

        def flush(self) -> None:
            if self._buffer.strip():
                self._logger.log(self._level, self._buffer.strip())
            self._buffer = ""

    project_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    script_path = os.path.join(
        project_root,
        "app",
        "scripts",
        "msd-auto-download",
        "msd-auto-download.py",
    )

    logger = logging.getLogger("msd_auto_download")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # prevent root-logger → stderr → _QueueWriter → logger loop
    logger.addHandler(_msd_queue_handler)

    stdout_writer = _QueueWriter(logger, logging.INFO)
    stderr_writer = _QueueWriter(logger, logging.ERROR)

    # ── stdin shim ────────────────────────────────────────────────────────────
    # Replace builtins.input inside the script. Handles two cases:
    #   • OTP prompt  — shows OTP panel in browser, waits for /otp
    #   • Re-login    — shows Re-login panel, waits for /relogin
    def _web_input(prompt: str = "") -> str:
        global _msd_otp_pending, _msd_relogin_pending
        if prompt == "__RELOGIN_WAIT__":
            # Session expired — emit sentinel, show Re-login panel, block
            logger.warning("__RELOGIN_REQUIRED__")  # picked up by SSE → re-login panel
            _msd_relogin_pending = True
            _msd_otp_queue.get()  # blocks until /relogin puts __RELOGIN_CONFIRMED__
            logger.info("Re-login confirmed by user.")
            return ""
        # Normal OTP flow
        if prompt:
            logger.info(prompt)
        logger.info("__OTP_REQUIRED__")   # sentinel picked up by SSE stream
        _msd_otp_pending = True
        code = _msd_otp_queue.get()       # blocks until /otp route puts a value
        logger.info("OTP code received from browser.")
        return code

    with app.app_context():
        try:
            logger.info("Starting MSD WO auto-download script...")
            original_argv = list(os.sys.argv)
            os.sys.argv = [script_path]
            with redirect_stdout(stdout_writer), redirect_stderr(stderr_writer):
                runpy.run_path(
                    script_path,
                    init_globals={"input": _web_input, "_MSD_RUN_ONCE": run_once},
                    run_name="__main__",
                )
            stdout_writer.flush()
            stderr_writer.flush()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
            if code == 0:
                logger.info("MSD WO auto-download finished.")
            else:
                logger.error("MSD WO auto-download exited with code %s.", code)
        except Exception:
            logger.error("MSD WO auto-download failed.")
            logger.error(traceback.format_exc())
        finally:
            global _msd_otp_pending, _msd_relogin_pending
            _msd_otp_pending = False
            _msd_relogin_pending = False
            os.sys.argv = original_argv
            logger.removeHandler(_msd_queue_handler)
            # Keep propagate=False permanently — this logger must never reach root


# ── Escalation Center page ────────────────────────────────────────────────────

@admin_bp.route("/admin/escalation-center", methods=["GET"])
def escalation_center():
    import os as _os
    project_root = _os.path.normpath(
        _os.path.join(_os.path.dirname(__file__), "..", "..")
    )
    xlsx_path = _os.path.join(project_root, "files", "source-db", "monday_link_map.xlsx")

    # Load board list from xlsx
    try:
        import sys
        scripts_dir = _os.path.join(project_root, "app", "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import monday_sync as _ms
        boards_raw = _ms.load_boards(xlsx_path)
    except Exception as exc:
        current_app.logger.warning("escalation_center: load_boards failed: %s", exc)
        boards_raw = []

    # Load sync state
    state_path = _os.path.join(project_root, "sync_state.json")
    try:
        import json as _json
        state = _json.loads(open(state_path, encoding="utf-8").read()) if _os.path.isfile(state_path) else {}
    except Exception:
        state = {}
    board_states = state.get("boards", {})

    board_meta = {}
    try:
        db = get_db()
        rows = db.execute(
            "SELECT monday_board_id, labor_vendor_related, customer_partner "
            "FROM asp_details WHERE monday_board_id IS NOT NULL"
        ).fetchall()
        board_meta = {
            str(row["monday_board_id"]): {
                "asp_id": row["labor_vendor_related"],
                "asp_name": row["customer_partner"],
            }
            for row in rows
        }
    except Exception:
        board_meta = {}

    # Enrich boards with per-board state
    boards = []
    for b in boards_raw:
        bid = str(b["board_id"])
        bs  = board_states.get(bid, {})
        meta = board_meta.get(bid, {})
        boards.append({
            "no":           b.get("no"),
            "asp_board":    b["asp_board"],
            "board_id":     bid,
            "asp_id":       meta.get("asp_id") or "—",
            "asp_name":     meta.get("asp_name") or "—",
            "last_sync":    bs.get("last_sync"),
            "total_synced": bs.get("total_synced", 0),
            "synced":       bool(bs.get("last_sync")),
        })

    # DB stats from lenovo_asp.db
    stats = {"items": 0, "updates": 0, "boards_synced": 0, "creators": 0}
    # latest_runs: dict keyed by board_id → most-recent sync_log row for that board
    latest_runs = {}
    edb = _get_monday_sync_db()
    if edb:
        try:
            stats["items"]        = edb.execute("SELECT COUNT(*) FROM technical_escalation").fetchone()[0]
            stats["updates"]      = edb.execute("SELECT COUNT(*) FROM item_updates").fetchone()[0]
            stats["boards_synced"] = edb.execute(
                "SELECT COUNT(DISTINCT board_id) FROM technical_escalation"
            ).fetchone()[0]
            stats["creators"]     = edb.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
            # One latest row per board_id — run_at shifted to WIB (UTC+7)
            rows = edb.execute(
                "SELECT s1.board_id, s1.asp_board, s1.run_type, s1.items_found, "
                "s1.items_upserted, s1.duration_sec, "
                "datetime(s1.run_at, '+7 hours') AS run_at, "
                "COALESCE(t.total_items, 0) AS total_items "
                "FROM sync_log s1 "
                "LEFT JOIN ("
                "  SELECT board_id, COUNT(*) AS total_items"
                "  FROM technical_escalation GROUP BY board_id"
                ") t ON t.board_id = s1.board_id "
                "WHERE s1.id = (SELECT MAX(id) FROM sync_log s2 WHERE s2.board_id = s1.board_id) "
                "ORDER BY s1.run_at DESC"
            ).fetchall()
            latest_runs = {str(r["board_id"]): dict(r) for r in rows}
        except Exception:
            pass
        finally:
            edb.close()

    boards.sort(key=lambda b: (b["asp_board"] or "").lower())
    board_count = len(boards)
    synced_count = sum(1 for b in boards if b["synced"])

    with _sync_lock:
        is_running = _sync_thread is not None and _sync_thread.is_alive()

    return render_template(
        "admin/escalation_center/monday_collector.html",
        portal="admin",
        active_page="monday_collector",
        active_group="escalation_center",
        boards=boards,
        board_count=board_count,
        synced_count=synced_count,
        stats=stats,
        latest_runs=latest_runs,
        is_running=is_running,
    )


# ── Escalation Center: trigger sync ─────────────────────────────────────────

@admin_bp.route("/admin/escalation-center/trigger", methods=["POST"])
def escalation_center_trigger():
    global _sync_thread
    data     = request.get_json(silent=True) or {}
    mode     = data.get("mode", "incremental")
    board_id = data.get("board_id")  # optional — None means all boards

    with _sync_lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return jsonify({"ok": False, "error": "Sync already running"}), 409

        _sync_stop.clear()
        app = current_app._get_current_object()
        _sync_thread = threading.Thread(
            target=_run_sync_task,
            args=(app, mode, board_id),
            daemon=True,
            name="monday-sync",
        )
        _sync_thread.start()

    return jsonify({"ok": True, "mode": mode, "board_id": board_id})


# ── Escalation Center: stop sync ─────────────────────────────────────────────

@admin_bp.route("/admin/escalation-center/stop", methods=["POST"])
def escalation_center_stop():
    _sync_stop.set()
    try:
        _log_queue.put_nowait({"ts": "", "level": "WARNING",
                               "msg": "Stop signal sent — sync will halt after current item."})
    except _queue.Full:
        try:
            _log_queue.get_nowait()
            _log_queue.put_nowait({"ts": "", "level": "WARNING",
                                   "msg": "Stop signal sent — sync will halt after current item."})
        except (_queue.Full, _queue.Empty):
            pass
    return jsonify({"ok": True})


# ── Escalation Center: SSE log stream ─────────────────────────────────────────

@admin_bp.route("/admin/escalation-center/stream", methods=["GET"])
def escalation_center_stream():
    def generate():
        import json as _json
        while True:
            try:
                rec = _log_queue.get(timeout=15)
                yield f"data: {_json.dumps(rec)}\n\n"
            except _queue.Empty:
                yield "data: {\"keepalive\": true}\n\n"
    return current_app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Escalation Center: status JSON ───────────────────────────────────────────

@admin_bp.route("/admin/escalation-center/status", methods=["GET"])
def escalation_center_status():
    import os as _os
    with _sync_lock:
        is_running = _sync_thread is not None and _sync_thread.is_alive()

    stats = {"items": 0, "updates": 0, "boards_synced": 0, "creators": 0}
    edb = _get_monday_sync_db()
    if edb:
        try:
            stats["items"]         = edb.execute("SELECT COUNT(*) FROM technical_escalation").fetchone()[0]
            stats["updates"]       = edb.execute("SELECT COUNT(*) FROM item_updates").fetchone()[0]
            stats["boards_synced"] = edb.execute(
                "SELECT COUNT(DISTINCT board_id) FROM technical_escalation"
            ).fetchone()[0]
            stats["creators"]      = edb.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
        except Exception:
            pass
        finally:
            edb.close()

    return jsonify({
        "ok":             True,
        "is_running":     is_running,
        "in_window":      _is_active_window(),
        "next_run_at":    _next_run_at,      # Unix ts of next scheduled sync (in-window idle)
        "window_opens_at": _window_opens_at, # Unix ts of next window open (off-hours)
        "stats":          stats,
    })


# ── Escalation Center: boards + history refresh ──────────────────────────────

@admin_bp.route("/admin/escalation-center/boards", methods=["GET"])
def escalation_center_boards():
    import os as _os, json as _json
    project_root = _os.path.normpath(
        _os.path.join(_os.path.dirname(__file__), "..", "..")
    )
    xlsx_path  = _os.path.join(project_root, "files", "source-db", "monday_link_map.xlsx")
    state_path = _os.path.join(project_root, "sync_state.json")

    # Board list
    try:
        import sys
        scripts_dir = _os.path.join(project_root, "app", "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import monday_sync as _ms
        boards_raw = _ms.load_boards(xlsx_path)
    except Exception:
        boards_raw = []

    # Sync state
    try:
        state = _json.loads(open(state_path, encoding="utf-8").read()) if _os.path.isfile(state_path) else {}
    except Exception:
        state = {}
    board_states = state.get("boards", {})

    board_meta = {}
    try:
        db = get_db()
        rows = db.execute(
            "SELECT monday_board_id, labor_vendor_related, customer_partner "
            "FROM asp_details WHERE monday_board_id IS NOT NULL"
        ).fetchall()
        board_meta = {
            str(row["monday_board_id"]): {
                "asp_id": row["labor_vendor_related"],
                "asp_name": row["customer_partner"],
            }
            for row in rows
        }
    except Exception:
        board_meta = {}

    boards = []
    for b in boards_raw:
        bid = str(b["board_id"])
        bs  = board_states.get(bid, {})
        meta = board_meta.get(bid, {})
        boards.append({
            "no":           b.get("no"),
            "asp_board":    b["asp_board"],
            "board_id":     bid,
            "asp_id":       meta.get("asp_id") or "—",
            "asp_name":     meta.get("asp_name") or "—",
            "last_sync":    bs.get("last_sync"),
            "total_synced": bs.get("total_synced", 0),
            "synced":       bool(bs.get("last_sync")),
        })

    boards.sort(key=lambda b: (b["asp_board"] or "").lower())

    # Latest run per board
    latest_runs = {}
    edb = _get_monday_sync_db()
    if edb:
        try:
            rows = edb.execute(
                "SELECT s1.board_id, s1.asp_board, s1.run_type, s1.items_found, "
                "s1.items_upserted, s1.duration_sec, "
                "datetime(s1.run_at, '+7 hours') AS run_at, "
                "COALESCE(t.total_items, 0) AS total_items "
                "FROM sync_log s1 "
                "LEFT JOIN ("
                "  SELECT board_id, COUNT(*) AS total_items"
                "  FROM technical_escalation GROUP BY board_id"
                ") t ON t.board_id = s1.board_id "
                "WHERE s1.id = (SELECT MAX(id) FROM sync_log s2 WHERE s2.board_id = s1.board_id) "
                "ORDER BY s1.run_at DESC"
            ).fetchall()
            latest_runs = {str(r["board_id"]): dict(r) for r in rows}
        except Exception:
            pass
        finally:
            edb.close()

    return jsonify({"ok": True, "boards": boards, "latest_runs": latest_runs})




# ── Monday Data page ──────────────────────────────────────────────────────────

@admin_bp.route("/admin/monday-data", methods=["GET"])
def monday_data():
    """Render the Monday Data page shell — data is loaded by the JS via API."""
    edb = _get_monday_sync_db()
    boards_list = []
    total_count = 0
    board_count = 0

    if edb:
        try:
            total_count = edb.execute(
                "SELECT COUNT(*) FROM technical_escalation"
            ).fetchone()[0]
            board_raw = edb.execute("""
                SELECT asp_board, board_id, COUNT(*) AS item_count
                FROM technical_escalation
                GROUP BY board_id
                ORDER BY asp_board
            """).fetchall()
            boards_list = [dict(r) for r in board_raw]
            board_count = len(boards_list)
        except Exception:
            pass
        finally:
            edb.close()

    return render_template(
        "admin/escalation_center/monday_data.html",
        portal="admin",
        active_page="monday_data",
        active_group="escalation_center",
        boards=boards_list,
        total_count=total_count,
        board_count=board_count,
    )


# ── Monday Data: JSON API ──────────────────────────────────────────────────────

@admin_bp.route("/admin/api/monday-data", methods=["GET"])
def monday_data_api():
    """Return all Monday sync rows as JSON — consumed by monday_data.js."""
    import json as _json
    edb = _get_monday_sync_db()
    rows_list   = []
    boards_list = []

    if edb:
        try:
            raw = edb.execute("""
                SELECT
                    te.monday_item_id,
                    te.board_id,
                    te.asp_board,
                    te.item_name,
                    te.item_created_at,
                    te.item_updated_at,
                    te.db_synced_at,
                    te.status,
                    te.work_order_type,
                    te.wo_case_id,
                    te.serial_number,
                    te.location,
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
                    c.creator_name,
                    COUNT(DISTINCT u.update_id) + COUNT(DISTINCT r.reply_id) AS disc_count
                FROM technical_escalation te
                LEFT JOIN creators c ON te.creator_id = c.creator_id
                LEFT JOIN item_updates u ON te.monday_item_id = u.monday_item_id
                LEFT JOIN item_update_replies r ON u.update_id = r.update_id
                GROUP BY te.monday_item_id
                ORDER BY te.item_created_at DESC
            """).fetchall()
            rows_list = [dict(r) for r in raw]

            board_raw = edb.execute("""
                SELECT asp_board, board_id, COUNT(*) AS item_count
                FROM technical_escalation
                GROUP BY board_id
                ORDER BY asp_board
            """).fetchall()
            boards_list = [dict(r) for r in board_raw]
        except Exception:
            pass
        finally:
            edb.close()

    board_names = {b["board_id"]: b["asp_board"] for b in boards_list}
    return jsonify({"rows": rows_list, "board_names": board_names})


# ── Monday Data: discussion JSON API ─────────────────────────────────────────

@admin_bp.route("/admin/monday-data/discussion/<item_id>", methods=["GET"])
def monday_data_discussion(item_id):
    edb = _get_monday_sync_db()
    result = {"updates": []}
    if not edb:
        return jsonify(result)

    try:
        # Load all updates for this item
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
            # Load replies for this update
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
    except Exception:
        pass
    finally:
        edb.close()

    return jsonify(result)
