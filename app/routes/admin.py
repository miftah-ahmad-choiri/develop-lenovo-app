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
from app.services.upload.meta_cache import write_meta, read_meta, delete_meta, mark_upserted
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

    return render_template("admin/data_import.html",
                           files=files,
                           category_rows=category_rows,
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
                # Upsert preview for WOID is not implemented yet.
                db_conn.close()
                return jsonify({"ok": False, "error": "Upsert preview for Work Order Advance Find View is not set up yet."})

            elif category_key == "SOID":
                # New rows = soid values in Excel not yet in wo_product_detail
                # AND work_order_id already in wo_summary
                valid_wo_ids = {
                    r[0] for r in db_conn.execute(
                        "SELECT work_order_id FROM wo_summary"
                    ).fetchall()
                }
                existing_soids = {
                    r[0] for r in db_conn.execute(
                        "SELECT soid FROM wo_product_detail"
                    ).fetchall()
                }
                wo_col   = "Work Order"
                line_col = "Line Order"
                date_col = "Created On"
                preview_cols = [wo_col, line_col, "Product", "Description",
                                date_col, "Work Order Product Status"]

                def _is_new_soid(row):
                    wo_id  = _safe_int(row.get(wo_col))
                    ln     = _safe_int(row.get(line_col))
                    soid   = _build_soid(wo_id, ln)
                    return (
                        soid is not None
                        and wo_id in valid_wo_ids
                        and soid not in existing_soids
                    )

                new_df = df[df.apply(_is_new_soid, axis=1)].copy() \
                    if (wo_col in df.columns and line_col in df.columns) \
                    else pd.DataFrame()

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
                preview_cols = [so_col, soid_col, "Ship PN", "Ship PN Desc",
                                date_col, "Company Name", "AWB", "Ship POU POD Time", "SLA"]

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

                def _is_impacted_shipment(row):
                    soid  = _safe_int(row.get(soid_col))
                    wo_id = _safe_int(row.get(so_col))
                    if soid is None or wo_id not in valid_wo_ids:
                        return False
                    db_row = db_shipment.get(soid)  # None if SOID not yet in DB
                    for excel_col, db_col in _excel_to_db.items():
                        db_val    = db_row[db_col] if db_row is not None else None
                        excel_val = row.get(excel_col)
                        # Real update: DB is NULL and Excel has an actual value
                        if db_val is None and _has_value(excel_val):
                            return True
                    return False

                new_df = df[df.apply(_is_impacted_shipment, axis=1)].copy() \
                    if (soid_col in df.columns and so_col in df.columns) \
                    else pd.DataFrame()
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

        return jsonify({
            "ok":               True,
            "category_key":     category_key,
            "filename":         target_file,
            "impacted_count":   impacted_count,
            "total_excel_rows": len(df),
            "date_col":         date_col,
            "preview_cols":     [c for c in preview_cols if c in df.columns],
            "all_rows":         _all_rows_sorted(new_df),
        })

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
