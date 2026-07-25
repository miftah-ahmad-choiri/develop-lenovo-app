"""
Excel file upload handler (source files for the WO pipeline).

Saves files to EXCEL_UPLOAD_FOLDER configured in the Flask app.
"""
import os
from flask import current_app
from werkzeug.utils import secure_filename

from app.services.upload.config import EXCEL_EXTENSIONS


def allowed_excel(filename: str) -> bool:
    """Return True if the filename has an allowed spreadsheet extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in EXCEL_EXTENSIONS


def save_excel_upload(file_storage) -> tuple[str | None, str | None]:
    """
    Save an uploaded Excel FileStorage to EXCEL_UPLOAD_FOLDER.
    Returns (saved_filename, error_message).
    """
    if not file_storage or not file_storage.filename:
        return None, "No file selected."
    if not allowed_excel(file_storage.filename):
        return None, f"Invalid file type. Allowed: {', '.join(f'.{e}' for e in EXCEL_EXTENSIONS)}"
    filename = secure_filename(file_storage.filename)
    dest_dir = current_app.config["EXCEL_UPLOAD_FOLDER"]
    os.makedirs(dest_dir, exist_ok=True)
    file_storage.save(os.path.join(dest_dir, filename))
    return filename, None


def list_excel_uploads() -> list[dict]:
    """Return a list of uploaded Excel files with metadata."""
    dest_dir = current_app.config["EXCEL_UPLOAD_FOLDER"]
    if not os.path.isdir(dest_dir):
        return []
    files = []
    for fname in sorted(os.listdir(dest_dir), reverse=True):
        if allowed_excel(fname) and not fname.endswith(".meta.json"):
            full = os.path.join(dest_dir, fname)
            stat = os.stat(full)
            files.append({
                "name": fname,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": stat.st_mtime,
            })
    return files
