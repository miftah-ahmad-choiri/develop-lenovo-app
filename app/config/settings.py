"""
Central configuration for the Flask application.

All paths are resolved relative to the repository root so they work
on any machine without hardcoding absolute paths.
"""
import os

# Repository root — two levels up from this file (app/config/settings.py → root)
_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


class Config:
    # ── Flask core ─────────────────────────────────────────────────────────────
    SECRET_KEY = "lenovo-asp-secret-key"
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    TEMPLATES_AUTO_RELOAD = True           # always re-read templates from disk

    # ── Session ────────────────────────────────────────────────────────────────
    # Sessions expire after 1 week; client-side JS signs out after 7 days
    PERMANENT_SESSION_LIFETIME = 604800         # seconds (7 days)

    # ── Upload folders ─────────────────────────────────────────────────────────
    # Evidence files (images / PDFs submitted with tickets)
    UPLOAD_FOLDER = os.path.join(_ROOT, "files", "upload")

    # Source Excel files uploaded before running the pipeline
    EXCEL_UPLOAD_FOLDER = os.path.join(_ROOT, "files", "upload", "excel")

    # Sidecar metadata JSON cache (written alongside each uploaded .xlsx)
    UPLOAD_META_FOLDER  = os.path.join(_ROOT, "app", "templates", "admin", "upload_meta")

    # ── Excel output ───────────────────────────────────────────────────────────
    EXCELS_DIR  = os.path.join(_ROOT, "files", "download", "excel")
    EXCEL_PATH  = os.path.join(_ROOT, "files", "download", "excel", "df_combined_final_report.xlsx")

    # ── Reports (exported xlsx files kept on disk) ──────────────────────────────
    REPORT_DIR  = os.path.join(_ROOT, "files", "report")

    # ── SQLite database ────────────────────────────────────────────────────────
    DATABASE_PATH = os.path.join(_ROOT, "files", "lenovo_asp.db")

    # ── Source DB seed files (initial load only) ───────────────────────────────
    SOURCE_DB_DIR = os.path.join(_ROOT, "files", "source-db")

    # ── MSD auto-download ───────────────────────────────────────────────────────
    # Seconds to wait after server startup before the first automatic MSD
    # download is triggered.  Reduce this for testing (e.g. 60 = 1 minute).
    MSD_STARTUP_DELAY_SEC: int = 10 * 60        # default: 10 minutes  (10 * 60)

    # Seconds between consecutive automatic MSD downloads in the repeating loop.
    # Reduce this for testing (e.g. 60 = 1 minute between runs).
    MSD_INTERVAL_SEC: int = 30 * 60           # default: 30 minutes

