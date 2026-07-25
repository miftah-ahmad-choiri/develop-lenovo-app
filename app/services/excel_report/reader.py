"""
Excel report reader for df_combined_final_report.xlsx.

Loads the report and maps raw column names to template field keys
using the mapping defined in config.py.
"""
import pandas as pd
from flask import current_app

from app.services.excel_report.config import COLUMN_MAP, DUPLICATE_MAP, FIXED_FIELDS


def _fmt(val) -> str:
    """Return a clean string from any cell value (handles NaN, NaT, datetime)."""
    if val is None:
        return ""
    if isinstance(val, float) and val != val:  # float NaN
        return ""
    try:
        if pd.isnull(val):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d %H:%M")
    return str(val).strip()


def load_wo_data() -> list[dict]:
    """Load work-order rows from df_combined_final_report.xlsx.

    Returns an empty list when the file does not exist yet (e.g. before
    a compiled report has been produced with the new database).
    """
    import os
    excel_path = current_app.config["EXCEL_PATH"]
    if not os.path.isfile(excel_path):
        return []

    df = pd.read_excel(excel_path, sheet_name=0)

    data = []
    for _, row in df.iterrows():
        record = {field: _fmt(row.get(col)) for col, field in COLUMN_MAP.items()}
        record.update({field: _fmt(row.get(col)) for field, col in DUPLICATE_MAP.items()})
        record.update(FIXED_FIELDS)
        data.append(record)
    return data
