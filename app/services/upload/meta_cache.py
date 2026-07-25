"""
Sidecar metadata cache for uploaded Excel files.

For every uploaded spreadsheet `<name>.xlsx` we write a tiny JSON file
`<name>.xlsx.meta.json` alongside it.  The GET /admin/data-import route
reads these cheap JSON files instead of re-parsing the full Excel workbook
on every page load.

Public API
----------
write_meta(folder, filename, result)   — called once after a successful upload
read_meta(folder, filename)            — called on every GET to build the file list
delete_meta(folder, filename)          — called when the Excel file is deleted
"""
from __future__ import annotations

import json
import os

_SUFFIX = ".meta.json"


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
