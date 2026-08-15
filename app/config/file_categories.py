"""
File-category detection configs used by upload verification and the DataFrame loader.

Each key is a category identifier.  Detection is done by column-matching — the
required_columns list must be a subset of the uploaded file's column headers for
the file to be considered a match.
"""

FILE_CATEGORY_CONFIGS = {
    "WOID": {
        "file_category": "Work Order Advance Find View",
        "source_file": "MSD",
        "required_columns": [
            "Created On", "Serial Number", "Case Number",
            "Work Order ID", "Product ID (MTM)", "Product Description", "Committed Delivery Date",
            "Actual Committed Onsite Date", "Completion Date", "Closing Date", "Case",
            "Order Type", "City", "Company Name", "Address 1 (Contact) (Contact)", 
            "Labor Vendor Related", "Work Order Status", "Customer (Labor Vendor Related) (Partner Function)",
            "Closing Code", "Case Status (Case) (Case)"
        ],
        "date_column": "Created On",
    },
    "SOID": {
        "file_category": "Work Order Product Advance Find View",
        "source_file": "MSD",
        "required_columns": [
            "Created On", "Work Order", "Work Order",
            "Product", "Description", "Acceptance Date", "Shipment Date", "Delivery Date",
            "Work Order Product Status"
        ],
        "date_column": "Created On",
    },
    "OPENORDER": {
        "file_category": "ID-IBM ID Open Order",
        "source_file": "Lenovo",
        "required_columns": [
            "Company Name", "Customer Name", "ETA WO can Close", "Is Customer Willing to Wait?",
            "Serial Number", "Service Delivery Instructions", "STATUS",
            "Status Update with Explanation", "Category", "Work Order ID", "Work Order Status",
            "WO Release Date",
        ],
        "date_column": "WO Release Date",
    },
    "SHIPMENT": {
        "file_category": "Lenovo Shipment Daily Report",
        "source_file": "YCH Logistics",
        "required_columns": [
            "Company Name", "Contact", "Service Provider ID", "Order Date", "SOID",
            "Service Delivery Type", "Ship PN", "Ship PN Desc", "Ship POU POD Time",
            "Ship To Address", "Ship To City", "SO", "Target",
        ],
        "date_column": "Order Date",
    },
    "PARTONHOLD": {
        "file_category": "Backlog Report File",
        "source_file": "Lenovo",
        "required_columns": [
            "ETA", "Machine SN", "Model", "Owner", "Part Number", "PN Desc",
            "Service Order ID", "SO ETA", "Status Date", "Service Order Creation Date",
        ],
        "date_column": "Service Order Creation Date",
    },
    "UNRETURN": {
        "file_category": "ID-IBM ID POU Unreturn",
        "source_file": "Lenovo",
        "required_columns": [
            "Ship PN", "Delivery Date", "WO Type", "AWB Number", "Labor Status",
            "Vendor Name", "Aging Days", "Vendor ID", "Note", "Return Status",
            "SO Completion Date", "DC/Collection Form", "Aging Range",
        ],
        "date_column": "SO Completion Date",
    },
    "GTAAP": {
        "file_category": "GTAAP Report",
        "source_file": "Resolv",
        "required_columns": [
            "Aging days", "DC#",
            "Labor Fix Date/time",
            "Part Return Date", "Service Provider ID", "SOID", "Status",
            "Return Flag", "WO#",
        ],
        "date_column": "Labor Fix Date/time",
    },
}
