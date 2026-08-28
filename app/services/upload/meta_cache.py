"""
Sidecar metadata cache for uploaded Excel files.

For every uploaded spreadsheet `<name>.xlsx` we write a tiny JSON file
`<name>.xlsx.meta.json` alongside it.  The GET /admin/data-import route
reads these cheap JSON files instead of re-parsing the full Excel workbook
on every page load.

A separate file `active_open_wos.json` in the same folder caches the list
of active (open, non-cancelled) Work Orders so the data-import page can
render it without hitting the database on every GET.

A second file `incomplete_prev_shipments.json` caches the list of SOIDs
from previous months that are Delivered but still missing AWB / Ship POU
POD Time.  Rebuilt after every SHIPMENT upsert.

Public API
----------
write_meta(folder, filename, result)       — called once after a successful upload
read_meta(folder, filename)                — called on every GET to build the file list
delete_meta(folder, filename)              — called when the Excel file is deleted
mark_upserted(folder, filename)            — marks a file as upserted in its sidecar

write_active_open_wos(folder, rows)        — persist active-WO list to JSON
read_active_open_wos(folder)               — load active-WO list from JSON ([] on miss)
rebuild_active_open_wos(folder, db_path)   — re-query DB and overwrite the cache

write_incomplete_prev_shipments(folder, rows)    — persist incomplete-shipment list
read_incomplete_prev_shipments(folder)           — load list from JSON ([] on miss)
rebuild_incomplete_prev_shipments(folder, db_path, excel_month) — re-query & overwrite

write_wo_product_mismatch(folder, rows)    — persist WO-product mismatch list to JSON
read_wo_product_mismatch(folder)           — load mismatch list from JSON ([] on miss)
"""
from __future__ import annotations

import json
import os
import sqlite3
from app.services.database.db import open_db

_SUFFIX = ".meta.json"
_ACTIVE_WO_FILE              = "active_open_wos.json"
_INCOMPLETE_SHIPMENTS_FILE   = "incomplete_prev_shipments.json"
_WO_PRODUCT_MISMATCH_FILE    = "wo_product_mismatch.json"


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
        conn = open_db(db_path)
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


# ── Incomplete previous-month shipments cache ─────────────────────────────────
# A single JSON file stores the list of SOIDs from previous months whose
# wo_product_status is 'Delivered', ship_pn is set, but awb and/or
# ship_pou_pod_time are still NULL/empty.
# Written once after every SHIPMENT upsert; read on every page GET.

_INCOMPLETE_SHIPMENTS_QUERY = """
    SELECT
        wpd.soid,
        wpd.work_order_id,
        wpd.ship_pn,
        wpd.wo_product_status,
        wpd.ship_pickup_time,
        wpd.awb,
        wpd.ship_pou_pod_time
    FROM wo_product_detail wpd
    WHERE wpd.ship_pn IS NOT NULL
      AND wpd.ship_pn != ''
      AND LOWER(COALESCE(wpd.wo_product_status, '')) = 'delivered'
      AND SUBSTR(COALESCE(wpd.ship_pickup_time, ''), 1, 7) < :excel_month
      AND SUBSTR(COALESCE(wpd.ship_pickup_time, ''), 1, 7) != ''
      AND (
            wpd.awb              IS NULL OR wpd.awb              = ''
         OR wpd.ship_pou_pod_time IS NULL OR wpd.ship_pou_pod_time = ''
      )
    ORDER BY wpd.ship_pickup_time ASC
"""


def write_incomplete_prev_shipments(folder: str, rows: list[dict]) -> None:
    """Persist *rows* as the incomplete-shipments cache JSON."""
    path = os.path.join(folder, _INCOMPLETE_SHIPMENTS_FILE)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    except OSError:
        pass


def read_incomplete_prev_shipments(folder: str) -> list[dict]:
    """Return the cached incomplete-shipments list, or [] on miss/corrupt."""
    path = os.path.join(folder, _INCOMPLETE_SHIPMENTS_FILE)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def rebuild_incomplete_prev_shipments(
    folder: str,
    db_path: str,
    excel_month: str,
) -> list[dict]:
    """
    Re-query the database for all previous-month Delivered SOIDs that are
    still missing AWB and/or Ship POU POD Time, write the result to the JSON
    cache, and return the list.

    *excel_month* is a "YYYY-MM" string derived from the dominant pickup month
    in the latest uploaded Shipment Excel (e.g. "2025-08").  Only SOIDs with
    ship_pickup_time strictly earlier than this month are considered.

    Call this once after every SHIPMENT upsert so the page cache stays fresh.
    """
    rows: list[dict] = []
    if not excel_month:
        return rows
    try:
        conn = open_db(db_path)
        try:
            db_rows = conn.execute(
                _INCOMPLETE_SHIPMENTS_QUERY,
                {"excel_month": excel_month},
            ).fetchall()
            for r in db_rows:
                _missing = []
                if not r[5] or str(r[5]).strip() == "":
                    _missing.append("AWB")
                if not r[6] or str(r[6]).strip() == "":
                    _missing.append("Ship POU POD Time")
                rows.append({
                    "Missing Fields":      "; ".join(_missing),
                    "SOID":                str(r[0]),
                    "SO (Work Order ID)":  str(r[1] or ""),
                    "Ship PN":             str(r[2] or ""),
                    "wo_product_status":   str(r[3] or ""),
                    "ship_pickup_time":    str(r[4] or "")[:10],
                    "AWB":                 str(r[5] or "") or "—",
                    "Ship POU POD Time":   str(r[6] or "") or "—",
                })
        finally:
            conn.close()
    except Exception:
        pass  # non-fatal — keep the old cache intact
    else:
        write_incomplete_prev_shipments(folder, rows)
    return rows


# ── WO-product mismatch cache ─────────────────────────────────────────────────
# Stores work_order_ids found in a "Work Order Product Advance Find View" Excel
# that do NOT exist in either wo_summary or wo_details in the database.
# Written each time the SOID upsert-preview runs; read on every page GET so the
# persistent warning card is shown without a DB hit.


def write_wo_product_mismatch(folder: str, rows: list[dict]) -> None:
    """Persist *rows* (list of dicts) as the WO-product mismatch cache JSON."""
    path = os.path.join(folder, _WO_PRODUCT_MISMATCH_FILE)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
    except OSError:
        pass


def read_wo_product_mismatch(folder: str) -> list[dict]:
    """Return the cached WO-product mismatch list, or [] if missing/corrupt."""
    path = os.path.join(folder, _WO_PRODUCT_MISMATCH_FILE)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []
