"""
Upload verification — reads every sheet of an uploaded spreadsheet with pandas,
runs detect_and_validate_dataframe() (column-based matching) against each sheet,
picks the best result, then returns a structured dict for the Flask /verify route.

Detection logic (your original script, unchanged):
  1. Exact match  → all required_columns present → "Validated"
  2. Partial match (≤ 3 missing cols) → "Unknown (Partial Match)"
  3. No match → "Unknown"

Supported formats: .xlsx, .xls, .csv
"""
from __future__ import annotations

import csv
import os
import warnings
from typing import Any

import pandas as pd

# Suppress noisy openpyxl warnings that appear on every file read:
#   - "Workbook contains no default style"
#   - "Data Validation extension is not supported and will be removed"
warnings.filterwarnings(
    "ignore",
    message=".*Workbook contains no default style.*",
    category=UserWarning,
    module="openpyxl",
)
warnings.filterwarnings(
    "ignore",
    message=".*Data Validation extension is not supported.*",
    category=UserWarning,
    module="openpyxl",
)

from app.config.file_categories import FILE_CATEGORY_CONFIGS

MAX_SAMPLE_ROWS = 5


# ── your original validation functions (verbatim) ─────────────────────────────

def perform_validation(df: pd.DataFrame, file_category_name: str,
                       source_file_name: str, required_cols_list: list,
                       date_col_for_analysis: str) -> dict:
    """
    Performs validation on a DataFrame based on a given file category configuration.
    Returns a dictionary with validation results.
    """
    missing_columns = [col for col in required_cols_list if col not in df.columns]

    if not missing_columns:
        result = {
            "validation_status": "Validated",
            "file_category": file_category_name,
            "source_file": source_file_name,
        }

        if date_col_for_analysis and date_col_for_analysis in df.columns:
            df_copy = df.copy()
            # Step 1 — parse to datetime, coercing errors to NaT.
            # format="mixed" silences the "Could not infer format" UserWarning
            # while still handling the mixed datetime formats MSD exports produce.
            parsed = pd.to_datetime(df_copy[date_col_for_analysis], errors="coerce", format="mixed")
            # Step 2 — strip timezone info if present.
            # All MSD/Lenovo/Resolv exports store datetimes as tz-naive local
            # (WIB) values in Excel — openpyxl reads them exactly as-is.
            # Only tz-aware Series (rare) need conversion; tz-naive is already
            # in the correct local time, so no UTC shift is applied.
            if hasattr(parsed, "dt") and parsed.dt.tz is not None:
                parsed = parsed.dt.tz_convert("Asia/Jakarta").dt.tz_localize(None)
            df_copy[date_col_for_analysis] = parsed
            df_cleaned = df_copy.dropna(subset=[date_col_for_analysis])

            if not df_cleaned.empty:
                oldest_date = df_cleaned[date_col_for_analysis].min()
                latest_date = df_cleaned[date_col_for_analysis].max()
                days_range = (latest_date - oldest_date).days
                result["latest_date"] = latest_date.strftime("%d-%m-%Y")
                result["days_range"] = f"{days_range} Days"
            else:
                result["date_analysis_note"] = (
                    f"'{date_col_for_analysis}' column is empty or contains "
                    "no valid dates after cleaning."
                )
        elif date_col_for_analysis:
            result["date_analysis_note"] = (
                f"Note: '{date_col_for_analysis}' column not found in DataFrame, "
                "skipping date range analysis."
            )

        return result

    else:
        return {
            "validation_status": "Invalid",
            "file_category": file_category_name,
            "source_file": source_file_name,
            "missing_columns": missing_columns,
            "message": (
                f"Specific Column '{', '.join(missing_columns)}' "
                "Please check file again and make sure upload the right file. Thanks"
            ),
        }


def detect_and_validate_dataframe(df: pd.DataFrame) -> dict:
    """
    Detects the file category of a DataFrame and performs validation.
    Returns a dictionary with validation status and details.
    """
    df_columns_set = set(df.columns)
    best_match = {
        "category_key": "Unknown",
        "match_count": -1,
        "total_required": -1,
        "missing_cols": [],
    }

    # First pass: exact match — all required columns present
    for category_key, config in FILE_CATEGORY_CONFIGS.items():
        required_cols_set = set(config["required_columns"])
        if required_cols_set.issubset(df_columns_set):
            return perform_validation(
                df,
                config["file_category"],
                config["source_file"],
                config["required_columns"],
                config["date_column"],
            )

    # Second pass: best partial match
    for category_key, config in FILE_CATEGORY_CONFIGS.items():
        required_cols_set = set(config["required_columns"])
        common_cols = df_columns_set.intersection(required_cols_set)
        match_count = len(common_cols)

        if match_count > best_match["match_count"]:
            best_match["match_count"] = match_count
            best_match["category_key"] = category_key
            best_match["total_required"] = len(required_cols_set)
            best_match["missing_cols"] = [
                col for col in required_cols_set if col not in df_columns_set
            ]
        elif (
            match_count == best_match["match_count"]
            and len(required_cols_set) < best_match["total_required"]
        ):
            best_match["match_count"] = match_count
            best_match["category_key"] = category_key
            best_match["total_required"] = len(required_cols_set)
            best_match["missing_cols"] = [
                col for col in required_cols_set if col not in df_columns_set
            ]

    if best_match["match_count"] > 0 and len(best_match["missing_cols"]) <= 3:
        config = FILE_CATEGORY_CONFIGS[best_match["category_key"]]
        return {
            "validation_status": "Unknown (Partial Match)",
            "file_category": config["file_category"],
            "source_file": config["source_file"],
            "missing_columns": best_match["missing_cols"],
            "message": (
                f"Specific Column '{', '.join(best_match['missing_cols'])}' "
                "Please check file again and make sure upload the right file. Thanks"
            ),
            "file_category_key": best_match["category_key"],
        }
    else:
        return {
            "validation_status": "Unknown",
            "message": (
                "Could not determine the file category based on its columns. "
                "Please upload the correct file!"
            ),
        }


# ── sheet reading helpers ─────────────────────────────────────────────────────

def _read_all_sheets_xlsx(filepath: str) -> list[tuple[str, pd.DataFrame]]:
    """Return [(sheet_name, df), ...] for every sheet in an xlsx/xls file.

    Two-pass strategy to avoid loading entire large sheets just for column
    detection (Backlog Report File has 1800+ rows across 4 sheets):

    Pass 1 — read only the header row (nrows=0) of every sheet to build a
              list of (sheet_name, columns) for detect_and_validate_dataframe.
              This is near-instant regardless of row count.
    Pass 2 — once the best sheet is identified in verify_uploaded_file, it
              is fully loaded there.  _read_all_sheets_xlsx returns stub
              DataFrames (0 rows) for non-winning sheets so the detection
              logic can still score them, but only the winning sheet is ever
              fully loaded.

    The full DataFrame for the winning sheet is attached back by
    verify_uploaded_file via a second xl.parse() call using the returned
    sheet name.
    """
    try:
        xl = pd.ExcelFile(filepath, engine="openpyxl")
    except Exception:
        xl = pd.ExcelFile(filepath)
    sheets = []
    try:
        for name in xl.sheet_names:
            try:
                # Read header only — zero data rows, fast on any sheet size
                df_header = xl.parse(name, header=0, nrows=0)
                sheets.append((name, df_header))
            except Exception:
                pass
    finally:
        xl.close()   # always release the file handle — critical on Windows
    return sheets


def _read_csv_sheet(filepath: str) -> list[tuple[str, pd.DataFrame]]:
    """Return a single-element list for a CSV file."""
    try:
        df = pd.read_csv(filepath, encoding="utf-8-sig", on_bad_lines="skip")
    except TypeError:
        df = pd.read_csv(filepath, encoding="utf-8-sig", error_bad_lines=False)
    return [("Sheet1", df)]


def _sample_rows(df: pd.DataFrame) -> list[list[Any]]:
    """Return up to MAX_SAMPLE_ROWS data rows as a list of lists (strings)."""
    sample = []
    for _, row in df.head(MAX_SAMPLE_ROWS).iterrows():
        sample.append([str(v) if v is not None and not (isinstance(v, float) and pd.isna(v)) else ""
                        for v in row])
    return sample


# ── public entry point ────────────────────────────────────────────────────────

def verify_uploaded_file(filepath: str) -> dict:
    """
    Read every sheet of *filepath*, run detect_and_validate_dataframe() on each,
    pick the sheet whose validation result is best (Validated > Partial > Unknown),
    and return a unified result dict for the Flask /verify route.

    Return shape (success):
    {
        "ok": True,
        "filename": "...",
        "size_kb": 42.1,
        "format": "xlsx" | "csv",
        "sheet_name": "Raw Data",
        "total_rows": 396,
        "total_cols": 73,
        "headers": [...],
        "sample_rows": [[...], ...],

        # from detect_and_validate_dataframe:
        "validation_status": "Validated" | "Unknown (Partial Match)" | "Unknown",
        "file_category": "ID-IBM ID Open Order",   # or None when Unknown
        "source_file": "Lenovo",                    # or None when Unknown
        "required_columns": [...],                  # or []
        "missing_columns": [...],                   # or []
        "message": "...",                           # only on non-Validated
        "latest_date": "16-07-2026",               # only when date present
        "days_range": "30 Days",                   # only when date present
    }

    Return shape (hard failure):
    {
        "ok": False,
        "filename": "...",
        "error": "..."
    }
    """
    if not os.path.isfile(filepath):
        return {"ok": False, "filename": os.path.basename(filepath), "error": "File not found."}

    filename = os.path.basename(filepath)
    size_kb = round(os.path.getsize(filepath) / 1024, 1)
    ext = filepath.rsplit(".", 1)[-1].lower()

    # ── load all sheets ───────────────────────────────────────────────────────
    try:
        if ext in ("xlsx", "xls"):
            fmt = "xlsx"
            sheets = _read_all_sheets_xlsx(filepath)
        elif ext == "csv":
            fmt = "csv"
            sheets = _read_csv_sheet(filepath)
        else:
            return {"ok": False, "filename": filename, "size_kb": size_kb,
                    "error": f"Unsupported extension: .{ext}"}
    except PermissionError:
        return {"ok": False, "filename": filename, "size_kb": size_kb,
                "error": (
                    f'The file "{filename}" is currently open in another program '
                    "(e.g. Microsoft Excel or OneDrive sync). "
                    "Please close it and try again."
                )}
    except Exception as exc:
        return {"ok": False, "filename": filename, "size_kb": size_kb,
                "error": f"Cannot open file: {exc}"}

    if not sheets:
        return {"ok": False, "filename": filename, "size_kb": size_kb,
                "error": "File contains no readable sheets."}

    # ── run detection on header-only stubs, pick the best sheet ─────────────
    # Priority: Validated > Unknown (Partial Match) > Unknown
    _priority = {"Validated": 2, "Unknown (Partial Match)": 1, "Unknown": 0}

    best_sheet_name = sheets[0][0]
    best_validation = detect_and_validate_dataframe(sheets[0][1])
    best_score = _priority.get(best_validation.get("validation_status", "Unknown"), 0)

    for sheet_name, df_stub in sheets[1:]:
        val = detect_and_validate_dataframe(df_stub)
        score = _priority.get(val.get("validation_status", "Unknown"), 0)
        if score > best_score:
            best_score = score
            best_sheet_name = sheet_name
            best_validation = val
        if best_score == 2:
            break  # Validated — no need to check further sheets

    # ── full read of the winning sheet only ───────────────────────────────────
    # _read_all_sheets_xlsx returned header-only stubs (nrows=0) so we must
    # do one full read here for the sample rows, row count, and date analysis.
    try:
        if ext in ("xlsx", "xls"):
            try:
                best_df = pd.read_excel(filepath, sheet_name=best_sheet_name,
                                        header=0, engine="openpyxl")
            except PermissionError:
                raise
            except Exception:
                best_df = pd.read_excel(filepath, sheet_name=best_sheet_name, header=0)
        else:
            best_df = pd.read_csv(filepath, encoding="utf-8-sig", on_bad_lines="skip")
    except PermissionError:
        return {"ok": False, "filename": filename, "size_kb": size_kb,
                "error": (
                    f'The file "{filename}" is currently open in another program '
                    "(e.g. Microsoft Excel or OneDrive sync). "
                    "Please close it and try again."
                )}
    except Exception as exc:
        return {"ok": False, "filename": filename, "size_kb": size_kb,
                "error": f"Cannot read sheet '{best_sheet_name}': {exc}"}

    # Re-run validation on the fully loaded DataFrame so date analysis works
    best_validation = detect_and_validate_dataframe(best_df)

    # ── build unified response ────────────────────────────────────────────────
    headers = [str(c) for c in best_df.columns.tolist()]
    sample = _sample_rows(best_df)
    total_rows = max(len(best_df), 0)

    # required_cols from the matched config (empty list when Unknown)
    required_cols: list[str] = []
    if best_validation.get("file_category"):
        for cfg in FILE_CATEGORY_CONFIGS.values():
            if cfg["file_category"] == best_validation["file_category"]:
                required_cols = cfg["required_columns"]
                break

    result: dict = {
        "ok": True,
        "filename": filename,
        "size_kb": size_kb,
        "format": fmt,
        "sheet_name": best_sheet_name,
        "total_rows": total_rows,
        "total_cols": len(headers),
        "headers": headers,
        "sample_rows": sample,
        "validation_status": best_validation.get("validation_status", "Unknown"),
        "file_category": best_validation.get("file_category"),
        "source_file": best_validation.get("source_file"),
        "required_columns": required_cols,
        "missing_columns": best_validation.get("missing_columns", []),
    }

    # optional fields
    for key in ("message", "latest_date", "days_range", "date_analysis_note"):
        if key in best_validation:
            result[key] = best_validation[key]

    return result
