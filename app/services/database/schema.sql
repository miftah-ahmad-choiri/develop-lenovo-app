-- ============================================================
-- Lenovo ASP — SQLite schema
-- Three core tables (focus scope: WO Summary, WO Details,
-- WO Product Detail).
--
-- wo_summary        : one row per Work Order (headline fields)
-- wo_details        : one row per Work Order (operational fields)
-- wo_product_detail : one row per part-order line (SOID),
--                     merging MSD product data + YCH shipment data
-- ============================================================

-- ------------------------------------------------------------
-- 1. wo_summary
--    Primary source : Work Order Summary.xlsx  (sheet WO_Summary)
--    Daily upload   : Work_Order_Advanced_Find_View_*.xlsx
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wo_summary (
    work_order_id               INTEGER PRIMARY KEY,  -- "Work Order ID"
    serial_number               TEXT,
    created_on                  TEXT,                 -- ISO datetime
    committed_delivery_date     TEXT,                 -- ISO datetime
    actual_committed_onsite_date TEXT,                -- ISO datetime
    case_desc                   TEXT,                 -- "Case" — complaint description
    work_order_type             TEXT,                 -- Onsite / Carry-In
    contact_name                TEXT,
    customer                    TEXT,                 -- ASP partner name
    work_order_status           TEXT,
    case_status                 TEXT                  -- "Case Status (Case) (Case)"
);

-- ------------------------------------------------------------
-- 2. wo_details
--    Primary source : Work Order Details.xlsx  (sheet WO_Details)
--    Daily upload   : Work_Order_Advanced_Find_View_*.xlsx
--    (same upload file as wo_summary — different column subset)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wo_details (
    work_order_id                   INTEGER PRIMARY KEY
                                        REFERENCES wo_summary(work_order_id),
    serial_number                   TEXT,
    case_number                     INTEGER,
    product_id_mtm                  TEXT,             -- machine type-model
    product_description             TEXT,             -- product name / model name
    release_date                    TEXT,
    original_committed_onsite_date  TEXT,
    customer_defer_date             TEXT,
    completion_date                 TEXT,
    closing_date                    TEXT,
    premier_service                 TEXT,             -- Premium Care / Legion Ultimate / null
    order_type                      TEXT,             -- Customer Limited Warranty / ADP
    work_order_priority             TEXT,             -- Premier / Normal
    city                            TEXT,
    company_name                    TEXT,
    address                         TEXT,
    mobile_phone                    TEXT,
    primary_email                   TEXT,
    labor_vendor_related            TEXT,             -- internal vendor ID
    technician_id                   TEXT,
    closing_code                    TEXT,
    repeat_repair                   TEXT,             -- Yes / No
    repeat_repair_reason            TEXT,
    wo_cancellation_reason          TEXT
);

-- Index for join from wo_summary → wo_details
CREATE INDEX IF NOT EXISTS idx_wo_details_work_order_id
    ON wo_details(work_order_id);

-- ------------------------------------------------------------
-- 3. wo_product_detail
--    Merged from TWO sources keyed on soid:
--
--    MSD source  : Work Order Product Details.xlsx
--                  soid = str(work_order_id) + str(line_order)
--                  e.g. work_order_id=4020720183, line_order=20
--                       → soid = 402072018320
--
--    YCH source  : Lenovo Shipment Daily Report.xlsx
--                  soid = native "SOID" column
--                  work_order_id = "SO" column
--
--    Both sources describe the same physical part-order line.
--    MSD columns are NULL until the MSD file is loaded/uploaded.
--    Shipment columns are NULL until the shipment file is loaded.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wo_product_detail (
    -- Key
    soid                INTEGER PRIMARY KEY,          -- part-order line ID
    work_order_id       INTEGER
                            REFERENCES wo_summary(work_order_id)
                            ON DELETE CASCADE,
    line_order          INTEGER,                      -- 20, 30, 40 … (from MSD)

    -- MSD columns (from Work Order Product Details / SOID upload)
    created_on          TEXT,                         -- ISO datetime
    product             TEXT,                         -- FRU / part number
    description         TEXT,                         -- human-readable part name
    acceptance_date     TEXT,                         -- nullable
    shipment_date       TEXT,                         -- nullable
    delivery_date       TEXT,                         -- nullable
    wo_product_status   TEXT,                         -- Delivered / Cancelled / Cancelled by Lenovo

    -- Shipment columns (from Lenovo Shipment Daily Report)
    order_date          TEXT,                         -- when SO raised in YCH system
    ship_pn             TEXT,                         -- part number shipped (may differ from product)
    ship_pn_desc        TEXT,
    return_flag         TEXT,                         -- Y / N
    ship_pickup_time    TEXT,                         -- nullable
    ship_pou_pod_time   TEXT,                         -- actual delivery timestamp
    awb                 TEXT,                         -- airwaybill number
    sla                 TEXT,                         -- NBD / 2BD etc.
    target              TEXT                          -- SLA deadline
);

-- Index for lookup by work_order_id (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_wo_product_detail_work_order_id
    ON wo_product_detail(work_order_id);
