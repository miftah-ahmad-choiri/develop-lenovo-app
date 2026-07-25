# SQLite Database Design Plan

## Top-Level Overview

Design and implement a SQLite database for the Lenovo ASP app with **three core tables**:

- **`wo_summary`** — from `Work Order Summary` source / `Work Order Advanced Find View` daily upload
- **`wo_details`** — from `Work Order Details` source / `Work Order Advanced Find View` daily upload
- **`wo_product_detail`** — merged table from BOTH `Work Order Product Details` (MSD) and `Lenovo Shipment Daily Report` (YCH Logistics), linked by a shared `soid` key

The database is seeded once from the four `files/source-db/` Excel files. After that, the daily upload cycle (files in `files/upload/excel/`) drives incremental upserts — no full reloads.

---

## Key Identifier Logic

The `work_order_id` (e.g. `4020720183`) is the **primary key** for `wo_summary` and `wo_details`, and a foreign key in `wo_product_detail`.

**`soid`** is the **primary key for `wo_product_detail`**, computed as:

```
soid = str(work_order_id) + str(line_order)
# Example: work_order_id=4020720183, line_order=20  →  soid=402072018320
```

This is identical to the `SOID` column in the Shipment Daily Report file. The `SO` column in the shipment file is simply the `work_order_id` (without the line_order suffix). This means both source files — `Work Order Product Details` and `Lenovo Shipment Daily Report` — describe the **same part-order line** and can be merged into one `wo_product_detail` table keyed on `soid`.

---

## Database Schema (per table)

### Table: `wo_summary`

Sourced from `Work Order Summary.xlsx` → sheet `WO_Summary`, and from `Work_Order_Advanced_Find_View_*.xlsx` daily uploads.

| Column | SQLite Type | Source Column | Note |
|---|---|---|---|
| `work_order_id` | INTEGER PRIMARY KEY | "Work Order ID" | Human-facing WO number |
| `wo_number` | TEXT | "(Do Not Modify) WO Number" | Internal GUID — nullable for some rows |
| `serial_number` | TEXT | "Serial Number" | Device serial number |
| `created_on` | TEXT | "Created On" | ISO datetime string |
| `modified_on` | TEXT | "(Do Not Modify) Modified On" | nullable |
| `committed_delivery_date` | TEXT | "Committed Delivery Date" | |
| `actual_committed_onsite_date` | TEXT | "Actual Committed Onsite Date" | |
| `case` | TEXT | "Case" | Complaint / problem description |
| `work_order_type` | TEXT | "Work Order Type" | Onsite / Carry-In |
| `contact_name` | TEXT | "Contact Name (Contact) (Contact)" | Customer person name |
| `customer` | TEXT | "Customer (Labor Vendor Related) (Partner Function)" | ASP partner name |
| `work_order_status` | TEXT | "Work Order Status" | e.g. Closed, RMA In Progress |

---

### Table: `wo_details`

Sourced from `Work Order Details.xlsx` → sheet `WO_Details`, and from `Work_Order_Advanced_Find_View_*.xlsx` daily uploads (same file as `wo_summary` — it contains all columns for both tables).

| Column | SQLite Type | Source Column | Note |
|---|---|---|---|
| `work_order_id` | INTEGER PRIMARY KEY | "Work Order ID" | FK → `wo_summary.work_order_id` |
| `wo_number` | TEXT | "(Do Not Modify) WO Number" | nullable |
| `modified_on` | TEXT | "(Do Not Modify) Modified On" | nullable |
| `serial_number` | TEXT | "Serial Number" | |
| `case_number` | INTEGER | "Case Number" | |
| `product_id_mtm` | TEXT | "Product ID (MTM)" | Machine type-model |
| `release_date` | TEXT | "Release Date" | |
| `original_committed_onsite_date` | TEXT | "Original Committed Onsite Date" | |
| `customer_defer_date` | TEXT | "Customer Defer Date" | nullable |
| `completion_date` | TEXT | "Completion Date" | nullable |
| `closing_date` | TEXT | "Closing Date" | nullable |
| `premier_service` | TEXT | "Premier Service" | Premium Care / Legion Ultimate / null |
| `order_type` | TEXT | "Order Type" | Customer Limited Warranty / ADP |
| `work_order_priority` | TEXT | "Work Order Priority" | Premier / Normal |
| `city` | TEXT | "City" | |
| `company_name` | TEXT | "Company Name" | End-customer company — nullable |
| `address` | TEXT | "Address 1 (Contact) (Contact)" | |
| `mobile_phone` | TEXT | "Mobile Phone (Contact) (Contact)" | |
| `primary_email` | TEXT | "Primary Email (Contact) (Contact)" | |
| `labor_vendor_related` | TEXT | "Labor Vendor Related" | Internal ASP vendor ID |
| `technician_id` | TEXT | "Technician ID" | |
| `closing_code` | TEXT | "Closing Code" | nullable |
| `case_status` | TEXT | "Case Status (Case) (Case)" | |
| `repeat_repair` | TEXT | "Repeat Repair" | Yes / No |
| `repeat_repair_reason` | TEXT | "Repeat Repair Reason" | nullable |
| `wo_cancellation_reason` | TEXT | "WO Cancellation Reason" | nullable |

---

### Table: `wo_product_detail`

**Merged table** — columns come from TWO source files that describe the same part-order line, joined on `soid`:

| Source File | Contributes |
|---|---|
| `Work Order Product Details.xlsx` (MSD) | `soid` (computed), `work_order_id`, `line_order`, `created_on`, `product`, `description`, `acceptance_date`, `shipment_date`, `delivery_date`, `wo_product_status` |
| `Lenovo Shipment Daily Report.xlsx` (YCH) | `soid` (native), `order_date`, `ship_pn`, `ship_pn_desc`, `return_flag`, `ship_pickup_time`, `ship_pou_pod_time`, `awb`, `sla`, `target` |

**SOID construction from MSD file:**
```
soid = int(str(work_order_id) + str(line_order))
# work_order_id=4020720183, line_order=20  →  soid=402072018320
```

**`SO` column in Shipment file = `work_order_id`** (the shipment `SO` is numerically equal to the MSD `Work Order` column).

| Column | SQLite Type | Source | Note |
|---|---|---|---|
| `soid` | INTEGER PRIMARY KEY | Shipment "SOID" / computed from MSD | Unique per part-order line |
| `work_order_id` | INTEGER | MSD "Work Order" / Shipment "SO" | FK → `wo_summary.work_order_id` |
| `line_order` | INTEGER | MSD "Line Order" | Position within WO (20, 30, 40…) |
| `created_on` | TEXT | MSD "Created On" | When the part order was created in MSD |
| `product` | TEXT | MSD "Product" | FRU / part number from MSD |
| `description` | TEXT | MSD "Description" | Human-readable part name from MSD |
| `acceptance_date` | TEXT | MSD "Acceptance Date" | nullable |
| `shipment_date` | TEXT | MSD "Shipment Date" | nullable |
| `delivery_date` | TEXT | MSD "Delivery Date" | nullable |
| `wo_product_status` | TEXT | MSD "Work Order Product Status" | Delivered / Cancelled / Cancelled by Lenovo |
| `order_date` | TEXT | Shipment "Order Date" | When the SO was raised in YCH system |
| `ship_pn` | TEXT | Shipment "Ship PN" | Part number shipped (may differ from MSD "Product") |
| `ship_pn_desc` | TEXT | Shipment "Ship PN Desc" | |
| `return_flag` | TEXT | Shipment "Return Flag" | Y / N |
| `ship_pickup_time` | TEXT | Shipment "Ship PickUp Time" | nullable |
| `ship_pou_pod_time` | TEXT | Shipment "Ship POU POD Time" | Actual delivery timestamp |
| `awb` | TEXT | Shipment "AWB" | Airwaybill number |
| `sla` | TEXT | Shipment "SLA" | NBD / 2BD etc. |
| `target` | TEXT | Shipment "Target" | SLA deadline |

> **Note on nulls:** MSD-only rows (part ordered but not yet in shipment system) will have all Shipment columns as NULL. Shipment-only rows (shipment exists but MSD row is not yet synced) will have MSD columns as NULL. Upsert logic handles both — a second upsert from the shipment file fills in the shipment columns for an existing `soid`.

---

## Sub-Tasks

---

### Sub-Task 1 — Create the SQLite database module

**Intent:** Establish the database file location, the `sqlite3` connection helper, and the `CREATE TABLE IF NOT EXISTS` migration script that creates all three tables with the correct schema and indexes.

**Expected Outcomes:**
- `app/services/database/db.py` exists with a `get_db()` helper that opens/returns the SQLite connection using a path from Flask config (`DATABASE_PATH`).
- `app/services/database/schema.sql` contains the full DDL for all three tables, plus indexes on `work_order_id` in `wo_details` and `wo_product_detail`.
- `app/services/database/migrate.py` has a `run_migrations(app)` function that executes the DDL.
- `Config` in [`app/config/settings.py`](app/config/settings.py) gains a `DATABASE_PATH` key pointing to `files/lenovo_asp.db`.
- `run_migrations(app)` is called from [`app/__init__.py`](app/__init__.py) `create_app()` so tables are created on first run.

**Todo List:**
1. Add `DATABASE_PATH` to [`app/config/settings.py`](app/config/settings.py).
2. Create `app/services/database/` directory with `__init__.py`.
3. Write `app/services/database/schema.sql` — DDL for `wo_summary`, `wo_details`, `wo_product_detail` plus indexes.
4. Write `app/services/database/db.py` with `get_db()` and `close_db()`.
5. Write `app/services/database/migrate.py` with `run_migrations(app)`.
6. Call `run_migrations(app)` from [`app/__init__.py`](app/__init__.py) `create_app()`.

**Relevant Context:**
- [`app/config/settings.py`](app/config/settings.py) — add `DATABASE_PATH`
- [`app/__init__.py`](app/__init__.py) — hook for `run_migrations`

**Status:** [ ] pending

---

### Sub-Task 2 — Write the seed loader (source-db → DB)

**Intent:** Read the four source Excel files from `files/source-db/` and bulk-insert them into the three DB tables. This runs once to populate the DB from historical data. The two product/shipment files are merged into a single `wo_product_detail` table using `soid` as the join key.

**Expected Outcomes:**
- `app/services/database/seed.py` with a `seed_from_source_db(app)` function.
- Idempotent — uses `INSERT OR IGNORE` so re-running it doesn't duplicate rows.
- `soid` is computed from MSD file as `int(str(work_order_id) + str(line_order))` before insert.
- The shipment file rows are upserted into the same table using their native `SOID` column — filling in shipment columns on already-inserted MSD rows, or creating new rows where no MSD row exists yet.
- Datetime cells are stored as ISO strings (`YYYY-MM-DD HH:MM:SS`).
- A CLI command `flask seed-db` triggers this function.

**Todo List:**
1. Create `app/services/database/seed.py`.
2. Map `Work Order Summary.xlsx` (sheet `WO_Summary`) → `wo_summary` column rename dict.
3. Map `Work Order Details.xlsx` (sheet `WO_Details`) → `wo_details` column rename dict.
4. Map `Work Order Product Details.xlsx` (sheet `Work Order Product Advanced...`) → `wo_product_detail`:
   - Compute `soid = int(str(work_order_id) + str(line_order))`.
   - Insert MSD columns; leave Shipment columns NULL.
   - Use `INSERT OR IGNORE` keyed on `soid`.
5. Map `Lenovo Shipment Daily Report.xlsx` (sheet `Sheet1`) → `wo_product_detail`:
   - Native `SOID` column = `soid`; native `SO` column = `work_order_id`.
   - Use `INSERT OR IGNORE` for new rows; then `UPDATE ... WHERE soid = ?` to fill in shipment columns on existing rows.
6. Write `seed_from_source_db(app)` that executes steps 2–5 in order within the Flask app context.
7. Register `flask seed-db` CLI command in [`app/__init__.py`](app/__init__.py).

**Relevant Context:**
- `work_order_id` from MSD may be a float (e.g. `4020720183.0`) — cast to `int`.
- `line_order` values are multiples of 10 (20, 30, 40…) — always a 2-digit suffix.
- The shipment `SO` column is numerically equal to `work_order_id`.
- Source files: `files/source-db/Work Order Summary.xlsx`, `files/source-db/Work Order Details.xlsx`, `files/source-db/Work Order Product Details.xlsx`, `files/source-db/Lenovo Shipment Daily Report.xlsx`.

**Status:** [ ] pending

---

### Sub-Task 3 — Write the daily upsert processor (upload → DB)

**Intent:** When a new file is uploaded via the existing upload pipeline, parse it and upsert the rows into the appropriate DB table. The `WOID` category writes to both `wo_summary` and `wo_details`. The `SOID` category writes to `wo_product_detail` (MSD columns only, computing `soid`). The `SHIPMENT` category writes to `wo_product_detail` (shipment columns only, using native `soid`).

**Expected Outcomes:**
- `app/services/database/upsert.py` with functions:
  - `upsert_wo_summary(df, conn)` — `INSERT OR REPLACE` keyed on `work_order_id`
  - `upsert_wo_details(df, conn)` — `INSERT OR REPLACE` keyed on `work_order_id`
  - `upsert_wo_product_from_msd(df, conn)` — computes `soid`, inserts MSD columns
  - `upsert_wo_product_from_shipment(df, conn)` — uses native `soid`, fills shipment columns
  - `dispatch_upsert(category_key, df, conn)` — routes to the correct function(s)
- `dispatch_upsert` is hooked into the existing upload pipeline so every verified upload also writes to the DB.

**Todo List:**
1. Create `app/services/database/upsert.py`.
2. Write `upsert_wo_summary(df, conn)`.
3. Write `upsert_wo_details(df, conn)`.
4. Write `upsert_wo_product_from_msd(df, conn)` — compute `soid`, `INSERT OR REPLACE` on MSD columns, leave shipment columns as NULL where not present.
5. Write `upsert_wo_product_from_shipment(df, conn)` — for each row, use `INSERT OR REPLACE` carrying all known columns (MSD columns will be NULL for shipment-only rows).
6. Write `dispatch_upsert(category_key, df, conn)`:
   - `"WOID"` → calls both `upsert_wo_summary` and `upsert_wo_details`
   - `"SOID"` → calls `upsert_wo_product_from_msd`
   - `"SHIPMENT"` → calls `upsert_wo_product_from_shipment`
7. Hook `dispatch_upsert` into the existing upload route (likely [`app/routes/excel_upload.py`](app/routes/excel_upload.py)) after verification passes.

**Relevant Context:**
- [`app/services/upload/excel_to_df.py`](app/services/upload/excel_to_df.py) — existing loader to hook into.
- [`app/routes/excel_upload.py`](app/routes/excel_upload.py) — where the dispatch hook should be triggered.
- [`app/config/file_categories.py`](app/config/file_categories.py) — category keys `WOID`, `SOID`, `SHIPMENT`.

**Status:** [ ] pending

---

### Sub-Task 4 — Expose DB query helpers for the ASP routes

**Intent:** Provide clean query functions over the three tables so Flask route handlers can render ASP pages from the database rather than from the legacy flat Excel file.

**Expected Outcomes:**
- `app/services/database/queries.py` with:
  - `get_all_wo_summary()` → list of dicts (joined with `wo_details`)
  - `get_wo_detail(work_order_id)` → single dict (full join of summary + details)
  - `get_parts_for_wo(work_order_id)` → list of dicts from `wo_product_detail`
  - `get_shipment_info(soid)` → single dict from `wo_product_detail`

**Todo List:**
1. Create `app/services/database/queries.py`.
2. Write `get_all_wo_summary()` — SELECT from `wo_summary` LEFT JOIN `wo_details` USING (`work_order_id`), ORDER BY `created_on DESC`.
3. Write `get_wo_detail(work_order_id)` — same join filtered to one WO.
4. Write `get_parts_for_wo(work_order_id)` — SELECT from `wo_product_detail` WHERE `work_order_id = ?` ORDER BY `line_order`.
5. Write `get_shipment_info(soid)` — SELECT from `wo_product_detail` WHERE `soid = ?`.

**Relevant Context:**
- Current data flow (to be replaced): [`app/services/excel_report/reader.py`](app/services/excel_report/reader.py) → `load_wo_data()`.
- ASP route handler: [`app/routes/asp.py`](app/routes/asp.py).
- ASP templates: [`app/templates/asp/dashboard.html`](app/templates/asp/dashboard.html), [`app/templates/asp/work_orders.html`](app/templates/asp/work_orders.html), [`app/templates/asp/parts_management.html`](app/templates/asp/parts_management.html).

**Status:** [ ] pending

---

## Key Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Primary key for WO tables | `work_order_id` INTEGER | Natural key, always present, stable across uploads |
| Primary key for `wo_product_detail` | `soid` INTEGER | Shared identifier between MSD and Shipment data — enables true merge |
| SOID construction | `int(str(work_order_id) + str(line_order))` | Matches the native `SOID` column in the Shipment file exactly |
| Merge strategy for product detail | Single table, two upsert passes | Avoids a JOIN view at query time; shipment columns default to NULL until shipment data arrives |
| Upsert strategy | `INSERT OR REPLACE` for WO tables; two-pass for product detail | WO data is always fully refreshed; product detail rows can be partially populated from either source |
| Datetime storage | TEXT as ISO strings | SQLite has no native datetime type; ISO format preserves sort order |
| DB file location | `files/lenovo_asp.db` | Co-located with other data files, outside the app package |
| Scope | `wo_summary`, `wo_details`, `wo_product_detail` only | Excluding action-log tables (escalation, reschedule, etc.) as requested |

---

## Data Relationship Diagram

```
wo_summary (work_order_id PK)
    │
    ├──── wo_details (work_order_id PK/FK)          [1-to-1]
    │
    └──<  wo_product_detail (soid PK, work_order_id FK)   [1-to-many]
              ▲
              │  soid computed from (work_order_id + line_order)
              │  OR native SOID from Shipment file
              │
          [MSD columns]          [Shipment columns]
          product, description   ship_pn, awb, sla
          acceptance_date        ship_pou_pod_time
          wo_product_status      return_flag, target …
```

`wo_summary` ↔ `wo_details` — **1-to-1** (same WO, different column sets from different MSD views).
`wo_summary` ↔ `wo_product_detail` — **1-to-many** (one WO can have multiple part-order lines).
`wo_product_detail` rows are populated from **two sources**: MSD product file (parts ordered) and YCH Shipment file (physical shipment tracking), merged on `soid`.
