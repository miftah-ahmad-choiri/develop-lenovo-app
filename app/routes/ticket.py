import json
from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.services.excel_report.reader import load_wo_data
from app.services.upload.evidence import allowed_file, save_upload

ticket_bp = Blueprint("ticket", __name__)


def _build_wo_context():
    """Load and partition WO data — shared by GET and POST."""
    wo_data   = load_wo_data()
    closed_wo = [w for w in wo_data if w["status"].lower() in ("completed", "closed")]
    open_wo   = [w for w in wo_data if w["status"].lower() not in ("completed", "closed", "cancelled", "canceled")]
    transit   = [w for w in wo_data if w["status_part"] and w["status_part"].lower() == "waiting part"]
    if not transit:
        transit = [w for w in wo_data if "part hold" in w["status"].lower()]
    part_hold = [w for w in wo_data if "part hold" in w["status"].lower()]

    return dict(
        wo_data=wo_data,
        open_wo=open_wo,
        transit=transit,
        closed_wo=closed_wo,
        # template vars
        wo_data_json=json.dumps(wo_data),
        open_wo_json=json.dumps(open_wo),
        transit_json=json.dumps(transit),
        closed_json=json.dumps(closed_wo),
        total=len(wo_data),
        total_closed=len(closed_wo),
        total_open=len(open_wo),
        total_transit=len(transit),
        # onsite / dashboard stats (same source file)
        onsite_total=len(wo_data),
        onsite_open=len(open_wo),
        onsite_part_hold=len(part_hold),
        onsite_closed=len(closed_wo),
    )


@ticket_bp.route("/", methods=["GET", "POST"])
def index():
    ctx = _build_wo_context()

    if request.method == "POST":
        errors = []
        wo_number     = request.form.get("wo_number", "").strip()
        tanggal_part  = request.form.get("tanggal_part", "").strip()
        ticket_status = request.form.get("ticket_status", "").strip()

        if not wo_number:
            errors.append("WO Number is required.")
        if not tanggal_part:
            errors.append("Tanggal part diterima oleh ASP is required.")
        if not ticket_status:
            errors.append("Ticket Status is required.")

        if ticket_status in ("Completed", "Completed but Not Solved"):
            if not request.form.get("wo_close_reason"):
                errors.append("WO Close reason is required.")
        if ticket_status == "Completed but Not Solved":
            if not request.form.get("not_solved_reason"):
                errors.append("WO close but not solved reason is required.")
        if ticket_status == "Reschedule":
            if not request.form.get("reschedule_by"):
                errors.append("Reschedule by Customer or ASP is required.")
            if not request.form.get("reschedule_date"):
                errors.append("Reschedule Date is required.")
            reschedule_by = request.form.get("reschedule_by", "")
            if reschedule_by == "ASP Defer" and not request.form.get("reschedule_reason_asp"):
                errors.append("Reschedule Reason by ASP Defer is required.")
            if reschedule_by == "Customer Defer" and not request.form.get("reschedule_reason_customer"):
                errors.append("Reschedule Reason by Customer Defer is required.")
        if ticket_status == "WO Canceled":
            if not request.form.get("canceled_reason", "").strip():
                errors.append("WO Canceled Reason is required.")

        uploaded_file = request.files.get("evidence")
        file_saved = None
        if uploaded_file and uploaded_file.filename:
            if allowed_file(uploaded_file.filename):
                file_saved = save_upload(uploaded_file)
            else:
                errors.append("Invalid file type. Allowed: PNG, JPG, JPEG, GIF, BMP, WEBP, PDF.")

        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template("index.html", form_data=request.form,
                                   active_tab="tab-ticket", **ctx)

        flash(
            f"Ticket submitted! WO: {wo_number}" + (f" | File: {file_saved}" if file_saved else ""),
            "success",
        )
        return redirect(url_for("ticket.index"))

    return render_template("index.html", form_data={},
                           active_tab="tab-ticket", **ctx)
