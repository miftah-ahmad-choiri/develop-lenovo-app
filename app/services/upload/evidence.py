"""
Evidence file upload handler (images / PDFs submitted with tickets).

Saves files to UPLOAD_FOLDER configured in the Flask app.
"""
import os
from flask import current_app
from werkzeug.utils import secure_filename

from app.services.upload.config import EVIDENCE_EXTENSIONS


def allowed_file(filename: str) -> bool:
    """Return True if the filename has an allowed evidence extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in EVIDENCE_EXTENSIONS


def save_upload(file_storage) -> str | None:
    """
    Save an uploaded FileStorage object to UPLOAD_FOLDER.
    Returns the saved filename, or None if no valid file was provided.
    """
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None
    filename = secure_filename(file_storage.filename)
    dest = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
    file_storage.save(dest)
    return filename
