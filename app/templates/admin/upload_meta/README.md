# upload_meta — Sidecar Metadata Cache

This directory contains lightweight JSON **sidecar files** for every Excel
spreadsheet uploaded via the **Data Import** page (`/admin/data-import`).

---

## Purpose

Parsing an Excel workbook on every page load is expensive.  When a file is
uploaded successfully the verification result is serialised into a tiny JSON
file here.  On subsequent GET requests the app reads this cheap JSON instead
of re-opening the spreadsheet, keeping the page fast regardless of file size.

---

## File Naming

Each sidecar mirrors the name of its source Excel file with a `.meta.json`
suffix appended:

```
<original_filename>.xlsx.meta.json
```

**Example:**

| Excel file (files/upload/excel/)                    | Sidecar (this folder)                               |
|-----------------------------------------------------|-----------------------------------------------------|
| `Backlog_Report_File_-_2026-07-16.xlsx`             | `Backlog_Report_File_-_2026-07-16.xlsx.meta.json`   |
| `20260716_ID-IBM_ID_Open_Order.xlsx`                | `20260716_ID-IBM_ID_Open_Order.xlsx.meta.json`      |

---

## JSON Schema

Every sidecar is a flat JSON object with five string fields:

```json
{
  "file_category":     "Backlog Report File",
  "source_file":       "Lenovo",
  "latest_date":       "16-07-2026",
  "days_range":        "282 Days",
  "validation_status": "Validated"
}
```

| Field              | Description                                                      |
|--------------------|------------------------------------------------------------------|
| `file_category`    | Logical category matched against `FILE_CATEGORY_CONFIGS`         |
| `source_file`      | Origin system / team (e.g. Lenovo, MSD, Resolv, YCH Logistics)  |
| `latest_date`      | Most-recent data date in the sheet (`dd-mm-yyyy`)                |
| `days_range`       | Approximate span of data rows (e.g. `"282 Days"`)                |
| `validation_status`| Result of header/schema validation (`"Validated"` or error text) |

---

## Lifecycle

| Event                   | Action on sidecar                                         |
|-------------------------|-----------------------------------------------------------|
| File uploaded           | `write_meta()` creates / overwrites the sidecar           |
| Page loaded (GET)       | `read_meta()` reads the sidecar; falls back to live parse |
| File deleted            | `delete_meta()` removes the sidecar                       |
| Reset all uploads       | `delete_meta()` called for every removed file             |

All three helpers live in [`app/services/upload/meta_cache.py`](../../../services/upload/meta_cache.py).
The folder path is configured as `UPLOAD_META_FOLDER` in [`app/config/settings.py`](../../../config/settings.py).
