import os
import sqlite3
import tempfile
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, current_app, send_file, jsonify,
)
from werkzeug.utils import secure_filename
from app.services.upload.excel import allowed_excel, save_excel_upload, list_excel_uploads
from app.services.upload.upload_verification import verify_uploaded_file
from app.services.upload.meta_cache import (
    write_meta, read_meta, delete_meta, mark_upserted,
    read_active_open_wos, rebuild_active_open_wos,
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

    return render_template("admin/data_import.html",
                           files=files,
                           category_rows=category_rows,
                           active_open_wos=active_open_wos,
                           portal="admin", active_page="data_import")


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
                date_col     = "Created On"
                preview_cols = [
                    "Upsert Reason",
                    "Work Order ID", "Serial Number", "Created On",
                    "Work Order Status", "Case Status (Case) (Case)",
                    "Closing Code", "Repeat Repair Reason", "WO Cancellation Reason",
                    "Completion Date", "Closing Date", "Committed Delivery Date",
                ]

                new_df = df[df.apply(_qualifies_woid, axis=1)].copy() \
                    if wo_id_col in df.columns \
                    else pd.DataFrame()

                # Inject synthetic "Upsert Reason" column
                if not new_df.empty:
                    new_df["Upsert Reason"] = new_df.apply(_reason_woid, axis=1).fillna("")

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
                date_col = "Acceptance Date"
                _soid_reason_col = "Upsert Reason"
                preview_cols = [
                    _soid_reason_col,
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
                else:
                    new_df = pd.DataFrame()

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

        # Return all impacted rows sorted by date_col descending (newest first)
        def _all_rows_sorted(frame):
            if frame.empty or date_col not in frame.columns:
                return []
            tmp = frame.copy()
            tmp["_sort_date"] = pd.to_datetime(tmp[date_col], errors="coerce")
            tmp = tmp.sort_values("_sort_date", ascending=False, na_position="last")
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

        # Use new_df for GTAAP, SOID, and SHIPMENT because they carry a synthetic
        # "Upsert Reason" column (or "DC# (will be inserted)") not in the raw df.
        _preview_col_source = new_df if (not new_df.empty and category_key in ("GTAAP", "SOID", "SHIPMENT")) else df
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
        # Rebuild the active-WO cache after a WOID upsert.
        # Pass the WO IDs from the Excel file so the cache only stores WOs
        # that were genuinely absent from this upload (matching the modal view).
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


# ── DataFrame Viewer ─────────────────────────────────────────────────────────

@admin_bp.route("/admin/data-import/view/<category_key>", methods=["GET"])
def data_import_view(category_key: str):
    """Render a full-table view for the uploaded file matching *category_key*.
    All rows are sent to the template; pagination and search are handled client-side.
    """
    category_key = category_key.upper()
    if category_key not in FILE_CATEGORY_CONFIGS:
        flash(f'Unknown category "{category_key}".', "danger")
        return redirect(url_for("admin.data_import"))

    df = load_single_dataframe(category_key)
    df_name = _KEY_TO_DF.get(category_key, "")
    label   = DF_LABELS.get(df_name, category_key)
    cfg     = FILE_CATEGORY_CONFIGS[category_key]

    if df is None:
        flash(f'No uploaded file found for category "{label}".', "warning")
        return redirect(url_for("admin.data_import"))

    headers = df.columns.tolist()
    rows    = df.values.tolist()

    return render_template(
        "admin/df_viewer.html",
        df_name=df_name,
        label=label,
        source_file=cfg.get("source_file", ""),
        headers=headers,
        rows=rows,
        total_rows=len(df),
        total_cols=len(headers),
        portal="admin",
        active_page="data_import",
    )


# ── Validation Center ────────────────────────────────────────────────────────

@admin_bp.route("/admin/validation", methods=["GET"])
def validation():
    return render_template("admin/validation_center.html",
                           portal="admin", active_page="validation")


# ── User & ASP Management ────────────────────────────────────────────────────

@admin_bp.route("/admin/users", methods=["GET"])
def users():
    return render_template("admin/user_management.html",
                           portal="admin", active_page="user_mgmt")


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
