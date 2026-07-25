"""
excel_to_df.py — Load uploaded Excel files into named pandas DataFrames.

Each uploaded file is matched to its category using the same column-detection
logic already used by upload_verification.  The seven known DataFrames are:

    df_woid       — Work Order Advanced Find View        (MSD)
    df_soid       — Work Order Product Advance Find View (MSD)
    df_openorder  — ID-IBM ID Open Order                 (Lenovo)
    df_shipment   — Lenovo Shipment Daily Report         (YCH Logistics)
    df_partonhold — Backlog Report File                  (Lenovo)
    df_unreturn   — ID-IBM ID POU Unreturn               (Lenovo)
    df_gtaap      — GTAAP Report                         (Resolv)

Usage::

    from app.services.upload.excel_to_df import load_all_dataframes
    dfs = load_all_dataframes()          # dict[str, pd.DataFrame | None]
    df_woid = dfs["df_woid"]

The function is request-aware (reads EXCEL_UPLOAD_FOLDER from the Flask app
config) so it must be called inside an application context.
"""

import os
import pandas as pd
from flask import current_app

from app.config.file_categories import FILE_CATEGORY_CONFIGS
from app.services.upload.excel import allowed_excel

# ── Mapping: file_category string → df_name ──────────────────────────────────
_CATEGORY_TO_DF: dict[str, str] = {
    "Work Order Advance Find View":        "df_woid",
    "Work Order Product Advance Find View": "df_soid",
    "ID-IBM ID Open Order":                "df_openorder",
    "Lenovo Shipment Daily Report":        "df_shipment",
    "Backlog Report File":                 "df_partonhold",
    "ID-IBM ID POU Unreturn":              "df_unreturn",
    "GTAAP Report":                        "df_gtaap",
}

# ── All known df_names in display order ──────────────────────────────────────
DF_NAMES_ORDERED = [
    "df_woid",
    "df_soid",
    "df_openorder",
    "df_shipment",
    "df_partonhold",
    "df_unreturn",
    "df_gtaap",
]

# ── Human-readable label for each df_name ────────────────────────────────────
DF_LABELS: dict[str, str] = {
    "df_woid":       "Work Order Advanced Find View",
    "df_soid":       "Work Order Product Advance Find View",
    "df_openorder":  "ID-IBM ID Open Order",
    "df_shipment":   "Lenovo Shipment Daily Report",
    "df_partonhold": "Backlog Report File",
    "df_unreturn":   "ID-IBM ID POU Unreturn",
    "df_gtaap":      "GTAAP Report",
}

# ── category_key (from file_categories) → df_name ────────────────────────────
_KEY_TO_DF: dict[str, str] = {
    "WOID":       "df_woid",
    "SOID":       "df_soid",
    "OPENORDER":  "df_openorder",
    "SHIPMENT":   "df_shipment",
    "PARTONHOLD": "df_partonhold",
    "UNRETURN":   "df_unreturn",
    "GTAAP":      "df_gtaap",
}


# ── category_key reverse lookup: file_category string → key ──────────────────
_FILE_CATEGORY_TO_KEY: dict[str, str] = {
    cfg["file_category"]: key
    for key, cfg in FILE_CATEGORY_CONFIGS.items()
}


def _detect_and_read(filepath: str) -> tuple[str | None, pd.DataFrame | None]:
    """
    Detect the category key AND read the correct sheet for *filepath* in one pass.

    verify_uploaded_file() already scans all sheets and returns the sheet_name
    where the data was found.  We reuse that sheet_name here so files whose data
    lives on a non-first sheet (e.g. OPENORDER → 'Raw Data', PARTONHOLD → 'raw data')
    are read correctly instead of falling back to sheet index 0.

    Returns (category_key, DataFrame) or (None, None) on any failure.
    """
    from app.services.upload.upload_verification import verify_uploaded_file

    result = verify_uploaded_file(filepath)
    if not result.get("ok"):
        return None, None
    file_category = result.get("file_category")
    if not file_category:
        return None, None

    key = _FILE_CATEGORY_TO_KEY.get(file_category)
    if key is None:
        return None, None

    sheet_name: str = result.get("sheet_name", "")
    ext = filepath.rsplit(".", 1)[-1].lower()
    try:
        if ext == "csv":
            df = pd.read_csv(filepath, dtype=str)
        elif sheet_name:
            df = pd.read_excel(filepath, sheet_name=sheet_name, dtype=str)
        else:
            df = pd.read_excel(filepath, dtype=str)
        return key, df
    except Exception:
        return None, None


def load_all_dataframes() -> dict[str, pd.DataFrame | None]:
    """
    Scan EXCEL_UPLOAD_FOLDER, detect each file's category, and return a dict
    mapping df_name → DataFrame (or None if the file is absent / unreadable).

    Must be called inside a Flask application context.
    """
    result: dict[str, pd.DataFrame | None] = {name: None for name in DF_NAMES_ORDERED}
    upload_folder: str = current_app.config["EXCEL_UPLOAD_FOLDER"]

    if not os.path.isdir(upload_folder):
        return result

    for fname in sorted(os.listdir(upload_folder)):
        if not allowed_excel(fname):
            continue
        filepath = os.path.join(upload_folder, fname)
        key, df = _detect_and_read(filepath)
        if key is None or df is None:
            continue
        df_name = _KEY_TO_DF.get(key)
        if df_name is None:
            continue
        # Only store the first (or only) file per category
        if result[df_name] is None:
            result[df_name] = df

    return result


def load_single_dataframe(category_key: str) -> pd.DataFrame | None:
    """
    Load and return the DataFrame for a single *category_key* (e.g. 'WOID').

    Must be called inside a Flask application context.
    Returns None when no matching file is found or the file cannot be read.
    """
    upload_folder: str = current_app.config["EXCEL_UPLOAD_FOLDER"]
    if not os.path.isdir(upload_folder):
        return None

    for fname in sorted(os.listdir(upload_folder)):
        if not allowed_excel(fname):
            continue
        filepath = os.path.join(upload_folder, fname)
        key, df = _detect_and_read(filepath)
        if key == category_key:
            return df

    return None
