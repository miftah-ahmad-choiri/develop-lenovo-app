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
    target              TEXT,                         -- SLA deadline (from Shipment file)
    eta_parthold_backlog TEXT,                        -- SO ETA from Backlog Report File (On Hold - Part Hold only)
    dc_number            TEXT                         -- DC# from GTAAP Report (Resolv), mapped by SOID
);

-- Index for lookup by work_order_id (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_wo_product_detail_work_order_id
    ON wo_product_detail(work_order_id);

-- ------------------------------------------------------------
-- 4. asp_details
--    Static reference table — one row per Authorized Service
--    Provider (ASP).  Seeded once from
--    files/source-db/asp table list.xlsx (Sheet1).
--    Not updated via the daily upload cycle.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asp_details (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    username                TEXT UNIQUE NOT NULL,   -- "Username"  e.g. asp001
    password                TEXT,                  -- "Password"
    vendor_code             TEXT,                  -- "Vendor Code"
    service_provider        TEXT,                  -- "SERVICE PROVIDER"
    parent_group            TEXT,                  -- "Parent Group"
    labor_vendor_related    TEXT,                  -- "Labor Vendor Related"
    customer_partner        TEXT,                  -- "Customer (Labor Vendor Related) (Partner Function)"
    store_name              TEXT,                  -- "Store Name"
    kota                    TEXT,                  -- "Kota"
    address                 TEXT,                  -- "Address"
    lat_long                TEXT,                  -- "LAT LONG"  raw combined string
    link_map                TEXT,                  -- "Link Map"
    phone_number            TEXT,                  -- "PHONE NUMBER"
    island                  TEXT,                  -- "Island"
    working_hours           TEXT,                  -- "WORKING HOURS"
    operational_status      TEXT,                  -- "Operational Status"
    future_status           TEXT,                  -- "Future Status"
    operation_support       TEXT,                  -- "Operation support"
    monday_board_id         TEXT,                  -- Monday.com board ID (from monday_link_map.xlsx)
    asp_id                  TEXT                   -- "ASP ID" from monday_link_map.xlsx (= labor_vendor_related)
);

CREATE INDEX IF NOT EXISTS idx_asp_details_username
    ON asp_details(username);

-- ------------------------------------------------------------
-- 5. admin_users
--    One row per admin / IBM-side portal user.
--    Separated from asp_details so ASP accounts and admin
--    accounts are never mixed in queries or the directory UI.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,   -- e.g. asp000, admin01
    password        TEXT,                  -- plain-text for now (to match existing pattern)
    full_name       TEXT,                  -- display name  e.g. "IBM Admin"
    email           TEXT,
    role            TEXT DEFAULT 'admin',  -- admin / superadmin / viewer …
    is_active       INTEGER DEFAULT 1,     -- 1 = active, 0 = disabled
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_admin_users_username
    ON admin_users(username);
