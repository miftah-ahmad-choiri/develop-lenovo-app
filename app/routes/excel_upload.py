import os
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, current_app,
)
from app.services.upload.excel import allowed_excel, save_excel_upload, list_excel_uploads

excel_upload_bp = Blueprint("excel_upload", __name__)


@excel_upload_bp.route("/upload-excel", methods=["GET", "POST"])
def upload_excel():
    if request.method == "POST":
        file = request.files.get("excel_file")

        if not file or not file.filename:
            flash("Please choose an Excel file to upload.", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        if not allowed_excel(file.filename):
            flash("Invalid file type. Allowed: .xlsx, .xls, .csv", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        saved, error = save_excel_upload(file)
        if error:
            flash(error, "danger")
        else:
            flash(f'File "{saved}" uploaded successfully.', "success")

        return redirect(url_for("excel_upload.upload_excel"))

    files = list_excel_uploads()
    for f in files:
        f["modified_fmt"] = datetime.fromtimestamp(f["modified"]).strftime("%Y-%m-%d %H:%M")

    return render_template("excel_upload.html", files=files)


@excel_upload_bp.route("/upload-excel/delete/<filename>", methods=["POST"])
def delete_excel(filename):
    from werkzeug.utils import secure_filename
    safe = secure_filename(filename)
    path = os.path.join(current_app.config["EXCEL_UPLOAD_FOLDER"], safe)
    if os.path.isfile(path):
        os.remove(path)
        flash(f'File "{safe}" deleted.', "success")
    else:
        flash("File not found.", "danger")
    return redirect(url_for("excel_upload.upload_excel"))


@excel_upload_bp.route("/upload-excel/reset", methods=["POST"])
def reset_excel():
    """Delete ALL files in the excel upload folder."""
    folder = current_app.config["EXCEL_UPLOAD_FOLDER"]
    deleted = 0
    for fname in os.listdir(folder):
        fpath = os.path.join(folder, fname)
        if os.path.isfile(fpath):
            os.remove(fpath)
            deleted += 1
    flash(f"Reset complete — {deleted} file{'s' if deleted != 1 else ''} deleted.", "success")
    return redirect(url_for("excel_upload.upload_excel"))


