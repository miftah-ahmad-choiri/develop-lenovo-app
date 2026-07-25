"""
Column mapping for the WO report reader.

Maps the raw Excel column names from df_combined_final_report.xlsx to the
field keys used by templates and API responses.
"""

# Maps raw Excel column name → template field key
COLUMN_MAP = {
    "WO#":                    "wo",
    "Actual ASP":             "contact",
    "CE Name":                "pic",
    "Creation Date":          "created_on",
    "WH Ship      (LAPS)":   "courier_pickup",
    "Part ETA Date":          "parts_eta",
    "Technician assign Date": "asp_received",
    "Fixed Date":             "wo_closed",
    "Status":                 "status",
    "Reason":                 "failed_reason",
    "Remarks":                "remark",
    "Customer Address":       "city",
    "Product":                "product",
    "SN":                     "sn",
    "Origin ASP":             "origin_asp",
    "Origin Vendor ID":       "origin_vendor",
    "Actual Vendor ID":       "actual_vendor",
    "Part Description":       "part_desc",
    "Status Part":            "status_part",
    "Customer Name":          "cust_name",
    "Customer Phone No ":     "cust_phone",
    "Aging":                  "aging",
    "Aging Category":         "aging_category",
    "KPI\n(Pass / Failed)":  "kpi",
    "Service Order Description": "svc_desc",
    "Information":            "information",
}

# Fields that share the same source column
DUPLICATE_MAP = {
    "order_date":   "Creation Date",   # same source as created_on
    "actual_asp":   "Actual ASP",      # same source as contact
}

# Fixed fields not sourced from a column
FIXED_FIELDS = {
    "month": "",
}
