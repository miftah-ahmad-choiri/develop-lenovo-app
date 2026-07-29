"""
Sidecar metadata cache for uploaded Excel files.

For every uploaded spreadsheet `<name>.xlsx` we write a tiny JSON file
`<name>.xlsx.meta.json` alongside it.  The GET /admin/data-import route
reads these cheap JSON files instead of re-parsing the full Excel workbook
on every page load.

A separate file `active_open_wos.json` in the same folder caches the list
of active (open, non-cancelled) Work Orders so the data-import page can
render it without hitting the database on every GET.

Public API
----------
write_meta(folder, filename, result)       — called once after a successful upload
read_meta(folder, filename)                — called on every GET to build the file list
delete_meta(folder, filename)              — called when the Excel file is deleted
mark_upserted(folder, filename)            — marks a file as upserted in its sidecar

write_active_open_wos(folder, rows)        — persist active-WO list to JSON
read_active_open_wos(folder)               — load active-WO list from JSON ([] on miss)
rebuild_active_open_wos(folder, db_path)   — re-query DB and overwrite the cache
"""
from __future__ import annotations

import json
import os
import sqlite3

_SUFFIX = ".meta.json"
_ACTIVE_WO_FILE = "active_open_wos.json"


def _meta_path(folder: str, filename: str) -> str:
    return os.path.join(folder, filename + _SUFFIX)


def write_meta(folder: str, filename: str, result: dict) -> None:
    """
    Persist the verification result dict as a JSON sidecar next to *filename*.

    Only the fields needed by the file-list page are stored; large per-row
    data (headers, sample_rows) is intentionally omitted to keep the file tiny.
    """
    payload = {
        "file_category":     result.get("file_category") or "",
        "source_file":       result.get("source_file") or "",
        "latest_date":       result.get("latest_date") or "",
        "days_range":        result.get("days_range") or "",
        "validation_status": result.get("validation_status") or "",
        "upserted":          bool(result.get("upserted")),
    }
    path = _meta_path(folder, filename)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass  # non-fatal — the GET handler falls back gracefully


def mark_upserted(folder: str, filename: str) -> None:
    """Set upserted=True in the sidecar for *filename* without touching other fields."""
    meta = read_meta(folder, filename)
    if meta is None:
        return
    meta["upserted"] = True
    path = _meta_path(folder, filename)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    except OSError:
        pass


def read_meta(folder: str, filename: str) -> dict | None:
    """
    Return the cached metadata dict, or *None* if the sidecar does not exist
    or cannot be parsed.
    """
    path = _meta_path(folder, filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def delete_meta(folder: str, filename: str) -> None:
    """Remove the sidecar file; silently ignored if it does not exist."""
    path = _meta_path(folder, filename)
    try:
        os.remove(path)
    except OSError:
        pass


# ── Active open WOs cache ─────────────────────────────────────────────────────
# A single JSON file stores the list of open (non-cancelled, no closing/completion
# date) Work Orders.  It is written once after every WOID upsert and read on
# every GET of the data-import page — no DB hit on the hot path.

_ACTIVE_WO_QUERY = (
    "SELECT s.work_order_id, s.serial_number, s.created_on, "
    "s.work_order_status, s.case_status, "
    "d.completion_date, d.closing_date, d.closing_code "
    "FROM wo_summary s "
    "LEFT JOIN wo_details d ON d.work_order_id = s.work_order_id "
    "WHERE (d.completion_date IS NULL OR d.completion_date = '') "
    "  AND (d.closing_date   IS NULL OR d.closing_date   = '') "
    "  AND LOWER(COALESCE(s.work_order_status, '')) NOT LIKE '%cancel%' "
    "  AND LOWER(COALESCE(s.case_status,       '')) NOT LIKE '%cancel%' "
    "ORDER BY s.created_on DESC"
)

_ACTIVE_WO_COLS = [
    "work_order_id", "serial_number", "created_on",
    "work_order_status", "case_status",
    "completion_date", "closing_date", "closing_code",
]


def write_active_open_wos(folder: str, rows: list[dict]) -> None:
    """Persist *rows* (list of dicts) as the active-WO cache JSON."""
    path = os.path.join(folder, _ACTIVE_WO_FILE)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    except OSError:
        pass


def read_active_open_wos(folder: str) -> list[dict]:
    """Return the cached active-WO list, or [] if the cache is missing/corrupt."""
    path = os.path.join(folder, _ACTIVE_WO_FILE)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def rebuild_active_open_wos(
    folder: str,
    db_path: str,
    excel_wo_ids: set[int] | None = None,
) -> list[dict]:
    """
    Re-query the database for all active open Work Orders, **exclude** those
    whose work_order_id appears in *excel_wo_ids* (i.e. they were present in
    the just-upserted Excel file), write the result to the JSON cache, and
    return the filtered list.

    The purpose of *excel_wo_ids* is to make the persistent page table show
    only WOs that were genuinely absent from the last upload — the same set
    shown in the upsert-preview modal's "Active WOs not in this Excel" section.

    Call this once after every WOID upsert so the page-load cache stays fresh.
    The DB already reflects the upserted rows, so WOs that received a
    completion_date or closing_date are automatically excluded by the query.
    """
    rows: list[dict] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            db_rows = conn.execute(_ACTIVE_WO_QUERY).fetchall()
            exclude = excel_wo_ids or set()
            rows = [
                dict(r) for r in db_rows
                if r["work_order_id"] not in exclude
            ]
        finally:
            conn.close()
    except Exception:
        pass  # non-fatal — keep the old cache intact if the query fails
    else:
        write_active_open_wos(folder, rows)
    return rows
