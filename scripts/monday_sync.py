"""
Monday.com → SQLite Smart Auto-Poller (Multi-Board)
Boards are loaded from monday_link.xlsx (columns: No, ASP_Board, Monday_board_id).

Smart polling strategy:
  - First run per board: full fetch of all items (paginated)
  - Subsequent runs: incremental fetch using updated_at watermark per board
  - Tracks last_sync per board in STATE_FILE keyed by board_id
  - Upserts rows (INSERT OR REPLACE) keyed on monday_item_id
  - Extracts structured fields from Diagnose note (Date/Time, Agent/CE, Model,
    Warranty Status, Problem Description) that are NOT visible in the board view
  - Records db_synced_at on every upsert so you always know when each row landed
  - Boards are synced sequentially (one at a time) in the order listed in the xlsx
"""

import html
import json
import logging
import os
import re
import re as _re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import requests

# ─── Configuration ───────────────────────────────────────────────────────────

MONDAY_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjY4MDU0NDA3MywiYWFpIjoxMSwidWlkIjo3ODQ3MzI4MywiaWFkIjoiMjAyNi0wNy0wOVQwNjo0NDoxMC4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6MTM1MzY5ODEsInJnbiI6InVzZTEifQ.PNnqcRTRrKG2JHDjWhaX7ocfEI923-HeSTeqnoydxNQ"
API_URL      = "https://api.monday.com/v2"
DB_FILE      = "technical_escalation.db"
STATE_FILE   = "sync_state.json"
XLSX_FILE    = "monday_link.xlsx"

# How often to poll (seconds).  Override with env var POLL_INTERVAL_SEC.
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "300"))   # default 5 min

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monday_sync")

# ─── GraphQL helpers ─────────────────────────────────────────────────────────

HEADERS = {
    "Authorization": MONDAY_TOKEN,
    "Content-Type":  "application/json",
    "API-Version":   "2024-01",
}

BOARD_META_QUERY = """
query BoardMeta($boardId: ID!) {
  boards(ids: [$boardId]) {
    columns { id title }
    groups { id title }
    items_count
  }
}
"""

ITEMS_QUERY = """
query FetchItems($boardId: ID!, $groupId: String!, $limit: Int!, $cursor: String) {
  boards(ids: [$boardId]) {
    groups(ids: [$groupId]) {
      id
      title
      items_page(limit: $limit, cursor: $cursor) {
        cursor
        items {
          id
          name
          group { id title }
          created_at
          updated_at
          creator { id name email }
          column_values {
            id text value type
          }
        }
      }
    }
  }
}
"""

UPDATES_QUERY = """
query FetchUpdates($itemId: ID!, $limit: Int!) {
  items(ids: [$itemId]) {
    updates(limit: $limit) {
      id
      body
      created_at
      updated_at
      creator { id name email }
      replies {
        id
        body
        created_at
        creator { id name email }
      }
    }
  }
}
"""


def gql(query: str, variables: dict) -> dict:
    """Execute a GraphQL query against Monday.com API with basic retry logic."""
    payload = {"query": query, "variables": variables}
    for attempt in range(1, 4):
        try:
            r = requests.post(API_URL, headers=HEADERS, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                log.error("GraphQL errors: %s", data["errors"])
                raise RuntimeError(data["errors"])
            return data
        except requests.RequestException as exc:
            log.warning("Request failed (attempt %d/3): %s", attempt, exc)
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                raise


def _incremental_limit(item_count: int) -> int:
    if item_count >= 500:
        return 100
    if item_count >= 100:
        return 50
    return 20


def _fetch_group_items(board_id: str, group: dict,
                       since_dt: datetime | None,
                       incremental: bool = False) -> list[dict]:
    group_id = group["id"]
    items: list[dict] = []
    cursor = None
    page   = 0

    page_limit = _incremental_limit(group.get("items_count") or 0) if incremental else 100

    while True:
        page += 1
        variables = {"boardId": board_id, "groupId": group_id,
                     "limit": page_limit, "cursor": cursor}
        data      = gql(ITEMS_QUERY, variables)
        page_data = data["data"]["boards"][0]["groups"][0]["items_page"]
        batch     = page_data["items"]
        cursor    = page_data.get("cursor")

        if since_dt:
            matching = [i for i in batch if
                        datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00")) >= since_dt]
            items.extend(matching)
        else:
            items.extend(batch)

        if incremental:
            break

        if not cursor:
            break

    return items


# ─── Board stats ─────────────────────────────────────────────────────────────

def _should_refresh_stats(conn: sqlite3.Connection, board_id: str) -> bool:
    row = conn.execute(
        "SELECT MIN(counted_at) AS oldest FROM board_stats WHERE board_id = ?",
        (board_id,)
    ).fetchone()
    if not row or not row["oldest"]:
        return True
    oldest = datetime.fromisoformat(row["oldest"].replace("Z", "+00:00"))
    age_days = (datetime.now(timezone.utc) - oldest).days
    return age_days >= 6


def refresh_board_stats(conn: sqlite3.Connection, board_id: str) -> None:
    meta        = gql(BOARD_META_QUERY, {"boardId": board_id})
    board_meta  = meta["data"]["boards"][0]
    groups      = [g for g in board_meta.get("groups", []) if (g.get("title") or "").strip() != "New Group"]
    board_total = board_meta.get("items_count") or 0
    num_groups  = max(len(groups), 1)
    est_per_group = board_total // num_groups
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for group in groups:
        conn.execute(
            "INSERT INTO board_stats (board_id, group_id, group_title, items_count, counted_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(board_id, group_id) DO UPDATE SET "
            "  group_title = excluded.group_title, "
            "  items_count = excluded.items_count, "
            "  counted_at  = excluded.counted_at",
            (board_id, group["id"], group["title"], est_per_group, now)
        )
    conn.execute(
        "DELETE FROM board_stats WHERE board_id = ? AND group_title = ?",
        (board_id, "New Group")
    )
    conn.commit()


def get_board_group_stats(conn: sqlite3.Connection, board_id: str) -> dict[str, int]:
    if _should_refresh_stats(conn, board_id):
        refresh_board_stats(conn, board_id)
    rows = conn.execute(
        "SELECT group_id, items_count FROM board_stats WHERE board_id = ? AND group_title <> ?",
        (board_id, "New Group")
    ).fetchall()
    return {r["group_id"]: r["items_count"] for r in rows}


def fetch_all_items(board_id: str, since: str | None = None,
                    conn: sqlite3.Connection | None = None) -> tuple[list[dict], dict[str, str]]:
    since_dt    = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    incremental = since_dt is not None

    meta        = gql(BOARD_META_QUERY, {"boardId": board_id})
    board_meta  = meta["data"]["boards"][0]
    col_map     = {c["title"]: c["id"] for c in board_meta.get("columns", [])}
    groups      = [g for g in board_meta.get("groups", []) if (g.get("title") or "").strip() != "New Group"]
    board_total = board_meta.get("items_count") or 0
    num_groups  = max(len(groups), 1)

    if conn is not None:
        group_counts = get_board_group_stats(conn, board_id)
    else:
        est = board_total // num_groups
        group_counts = {g["id"]: est for g in groups}

    all_items: list[dict] = []
    group_summaries: list[dict[str, int | str]] = []
    for group in groups:
        cnt              = group_counts.get(group["id"], board_total // num_groups)
        group_with_count = dict(group, items_count=cnt)
        group_items      = _fetch_group_items(board_id, group_with_count, since_dt,
                                              incremental=incremental)
        all_items.extend(group_items)
        group_summaries.append({
            "title": group.get("title") or "(untitled group)",
            "items": len(group_items),
        })

    return all_items, col_map, group_summaries


# ─── Board list ──────────────────────────────────────────────────────────────

def load_boards(xlsx_path: str = XLSX_FILE) -> list[dict]:
    import shutil, tempfile
    try:
        # Copy to a temp file so an Excel/OneDrive lock on the original
        # doesn't block the read.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            tmp_path = tmp.name
        shutil.copy2(xlsx_path, tmp_path)
        wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
    except PermissionError:
        raise PermissionError(
            f"{xlsx_path} is open in another program (e.g. Excel). "
            f"Please close it and try again."
        )
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    try:
        Path(tmp_path).unlink(missing_ok=True)
    except Exception:
        pass

    if not rows:
        raise ValueError(f"No rows found in {xlsx_path}")

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    try:
        col_no   = header.index("No")
        col_name = header.index("ASP_Board")
        col_id   = header.index("Monday_board_id")
    except ValueError as exc:
        raise ValueError(
            f"Expected columns 'No', 'ASP_Board', 'Monday_board_id' in {xlsx_path}. "
            f"Found: {header}"
        ) from exc

    boards = []
    for row in rows[1:]:
        no       = row[col_no]
        asp_name = row[col_name]
        board_id = row[col_id]
        if board_id is None or asp_name is None:
            continue
        boards.append({
            "no":        int(no) if no is not None else None,
            "asp_board": str(asp_name).strip(),
            "board_id":  str(int(board_id)),
        })

    if not boards:
        raise ValueError(f"No valid board rows found in {xlsx_path}")

    log.info("Loaded %d boards from %s", len(boards), xlsx_path)
    return boards


# ─── State persistence ────────────────────────────────────────────────────────

def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        if "boards" not in data:
            data["boards"] = {}
        return data
    return {"boards": {}, "last_sync": None, "total_synced": 0}


def save_state(state: dict) -> None:
    Path(STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_board_state(state: dict, board_id: str) -> dict:
    if board_id not in state["boards"]:
        state["boards"][board_id] = {"last_sync": None, "total_synced": 0}
    return state["boards"][board_id]


# ─── Database setup ───────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    creator_id    TEXT PRIMARY KEY,
    creator_name  TEXT,
    creator_email TEXT
);

CREATE TABLE IF NOT EXISTS technical_escalation (
    db_synced_at        TEXT,
    item_updated_at     TEXT,
    board_id            TEXT,
    asp_board           TEXT,
    monday_item_id      TEXT PRIMARY KEY,
    item_name           TEXT,
    item_created_at     TEXT,
    status              TEXT,
    work_order_type     TEXT,
    wo_case_id          TEXT,
    serial_number       TEXT,
    diagnose_note       TEXT,
    repair_note         TEXT,
    ppsn_category       TEXT,
    rrr_category        TEXT,
    location            TEXT,
    creator_id          TEXT REFERENCES creators(creator_id),
    diag_datetime       TEXT,
    diag_agent_ce       TEXT,
    diag_model          TEXT,
    diag_warranty       TEXT,
    diag_problem        TEXT,
    diag_esc_approval   TEXT,
    diag_parts_request  TEXT
);

CREATE INDEX IF NOT EXISTS idx_item_updated_at ON technical_escalation(item_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_status          ON technical_escalation(status);
CREATE INDEX IF NOT EXISTS idx_serial_number   ON technical_escalation(serial_number);
CREATE INDEX IF NOT EXISTS idx_wo_case_id      ON technical_escalation(wo_case_id);
CREATE INDEX IF NOT EXISTS idx_creator_id      ON technical_escalation(creator_id);
"""

_POST_MIGRATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_monday_item_id ON technical_escalation(monday_item_id);
CREATE INDEX IF NOT EXISTS idx_board_id        ON technical_escalation(board_id);
CREATE INDEX IF NOT EXISTS idx_upd_item_id    ON item_updates(monday_item_id);
CREATE INDEX IF NOT EXISTS idx_upd_created_at ON item_updates(created_at DESC);
"""

UPDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_updates (
    update_id          TEXT PRIMARY KEY,
    monday_item_id     TEXT REFERENCES technical_escalation(monday_item_id),
    body_text          TEXT,
    created_at         TEXT,
    updated_at         TEXT,
    creator_id         TEXT REFERENCES creators(creator_id)
);

CREATE TABLE IF NOT EXISTS item_update_replies (
    reply_id        TEXT PRIMARY KEY,
    update_id       TEXT REFERENCES item_updates(update_id),
    body_text       TEXT,
    created_at      TEXT,
    creator_id      TEXT REFERENCES creators(creator_id)
);

CREATE INDEX IF NOT EXISTS idx_rep_update_id  ON item_update_replies(update_id);
"""

SYNC_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    board_id    TEXT,
    asp_board   TEXT,
    run_type    TEXT NOT NULL,
    items_found INTEGER,
    items_upserted INTEGER,
    duration_sec REAL
);
"""

BOARD_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_stats (
    board_id        TEXT    NOT NULL,
    group_id        TEXT    NOT NULL,
    group_title     TEXT,
    items_count     INTEGER NOT NULL DEFAULT 0,
    counted_at      TEXT    NOT NULL,
    PRIMARY KEY (board_id, group_id)
);
"""


def _needs_migration(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(technical_escalation)")}
    stale = {"date", "person_names", "status_changed_at",
             "person_changed_at", "date_changed_at", "creator_name", "creator_email"}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    upd_cols = ({r[1] for r in conn.execute("PRAGMA table_info(item_updates)")}
                if "item_updates" in tables else set())
    rep_cols = ({r[1] for r in conn.execute("PRAGMA table_info(item_update_replies)")}
                if "item_update_replies" in tables else set())
    missing_board_cols = "board_id" not in cols or "asp_board" not in cols
    item_name_is_pk = any(
        row[1] == "item_name" and row[5] == 1
        for row in conn.execute("PRAGMA table_info(technical_escalation)")
    )
    return (bool(cols & stale)
            or "creators" not in tables
            or "monday_item_id" not in cols
            or missing_board_cols
            or item_name_is_pk
            or "item_name" in upd_cols
            or "body_html" in upd_cols
            or "body_html" in rep_cols)


def _needs_sync_log_migration(conn: sqlite3.Connection) -> bool:
    sync_log_cols = {r[1] for r in conn.execute("PRAGMA table_info(sync_log)")}
    return "board_id" not in sync_log_cols or "asp_board" not in sync_log_cols


def get_db(path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(UPDATES_SCHEMA)
    conn.executescript(SYNC_LOG_SCHEMA)
    conn.executescript(BOARD_STATS_SCHEMA)
    if _needs_migration(conn):
        log.info("Migrating table schema...")
        conn.executescript(_MIGRATE_SQL)
        log.info("Migration complete.")
    item_name_is_pk = any(
        row[1] == "item_name" and row[5] == 1
        for row in conn.execute("PRAGMA table_info(technical_escalation)")
    )
    if item_name_is_pk:
        log.info("Migrating PRIMARY KEY...")
        conn.executescript(_MIGRATE_PK_SQL)
        log.info("PK migration complete.")
    if _needs_sync_log_migration(conn):
        log.info("Migrating sync_log schema...")
        try:
            conn.executescript(_MIGRATE_SYNC_LOG_SQL)
        except Exception:
            pass
    conn.executescript(_POST_MIGRATE_INDEXES)
    conn.commit()
    return conn


_MIGRATE_SQL = """
CREATE TABLE IF NOT EXISTS creators (
    creator_id    TEXT PRIMARY KEY,
    creator_name  TEXT,
    creator_email TEXT
);
CREATE TABLE IF NOT EXISTS technical_escalation_new (
    db_synced_at TEXT, item_updated_at TEXT, board_id TEXT, asp_board TEXT,
    monday_item_id TEXT PRIMARY KEY, item_name TEXT, item_created_at TEXT,
    status TEXT, work_order_type TEXT, wo_case_id TEXT, serial_number TEXT,
    diagnose_note TEXT, repair_note TEXT, ppsn_category TEXT, rrr_category TEXT,
    location TEXT, creator_id TEXT REFERENCES creators(creator_id),
    diag_datetime TEXT, diag_agent_ce TEXT, diag_model TEXT, diag_warranty TEXT,
    diag_problem TEXT, diag_esc_approval TEXT, diag_parts_request TEXT
);
INSERT OR IGNORE INTO technical_escalation_new
    SELECT db_synced_at, item_updated_at, NULL, NULL, monday_item_id,
           item_name, item_created_at, status, work_order_type, wo_case_id,
           serial_number, diagnose_note, repair_note, ppsn_category, rrr_category,
           location, creator_id, diag_datetime, diag_agent_ce, diag_model,
           diag_warranty, diag_problem, diag_esc_approval, diag_parts_request
    FROM technical_escalation;
DROP TABLE technical_escalation;
ALTER TABLE technical_escalation_new RENAME TO technical_escalation;
CREATE INDEX IF NOT EXISTS idx_monday_item_id  ON technical_escalation(monday_item_id);
CREATE INDEX IF NOT EXISTS idx_item_updated_at ON technical_escalation(item_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_status          ON technical_escalation(status);
CREATE INDEX IF NOT EXISTS idx_board_id        ON technical_escalation(board_id);
CREATE TABLE IF NOT EXISTS item_updates_new (
    update_id TEXT PRIMARY KEY,
    monday_item_id TEXT REFERENCES technical_escalation(monday_item_id),
    body_text TEXT, created_at TEXT, updated_at TEXT,
    creator_id TEXT REFERENCES creators(creator_id)
);
DROP TABLE IF EXISTS item_updates;
ALTER TABLE item_updates_new RENAME TO item_updates;
CREATE TABLE IF NOT EXISTS item_update_replies_new (
    reply_id TEXT PRIMARY KEY,
    update_id TEXT REFERENCES item_updates(update_id),
    body_text TEXT, created_at TEXT,
    creator_id TEXT REFERENCES creators(creator_id)
);
INSERT OR IGNORE INTO item_update_replies_new
    SELECT reply_id, update_id, body_text, created_at, creator_id
    FROM item_update_replies;
DROP TABLE IF EXISTS item_update_replies;
ALTER TABLE item_update_replies_new RENAME TO item_update_replies;
"""

_MIGRATE_PK_SQL = """
CREATE TABLE IF NOT EXISTS technical_escalation_new (
    db_synced_at TEXT, item_updated_at TEXT, board_id TEXT, asp_board TEXT,
    monday_item_id TEXT PRIMARY KEY, item_name TEXT, item_created_at TEXT,
    status TEXT, work_order_type TEXT, wo_case_id TEXT, serial_number TEXT,
    diagnose_note TEXT, repair_note TEXT, ppsn_category TEXT, rrr_category TEXT,
    location TEXT, creator_id TEXT REFERENCES creators(creator_id),
    diag_datetime TEXT, diag_agent_ce TEXT, diag_model TEXT, diag_warranty TEXT,
    diag_problem TEXT, diag_esc_approval TEXT, diag_parts_request TEXT
);
INSERT OR IGNORE INTO technical_escalation_new
    SELECT db_synced_at, item_updated_at, board_id, asp_board, monday_item_id,
           item_name, item_created_at, status, work_order_type, wo_case_id,
           serial_number, diagnose_note, repair_note, ppsn_category, rrr_category,
           location, creator_id, diag_datetime, diag_agent_ce, diag_model,
           diag_warranty, diag_problem, diag_esc_approval, diag_parts_request
    FROM technical_escalation WHERE monday_item_id IS NOT NULL;
DROP TABLE technical_escalation;
ALTER TABLE technical_escalation_new RENAME TO technical_escalation;
"""

_MIGRATE_SYNC_LOG_SQL = """
ALTER TABLE sync_log ADD COLUMN board_id  TEXT;
ALTER TABLE sync_log ADD COLUMN asp_board TEXT;
"""


# ─── Data extraction ──────────────────────────────────────────────────────────

def _cv(column_values: list[dict], col_id: str) -> tuple[str | None, str | None]:
    for cv in column_values:
        if cv["id"] == col_id:
            return cv.get("text"), cv.get("value")
    return None, None


def _cv_by_title(column_values: list[dict], col_map: dict[str, str],
                 title: str) -> tuple[str | None, str | None]:
    title_lower = title.lower()
    col_id = next((v for k, v in col_map.items() if k.lower() == title_lower), None)
    if not col_id:
        return None, None
    return _cv(column_values, col_id)


_DIAG_PATTERNS = {
    "diag_datetime":     re.compile(r"Date/Time\s*[:：]\s*(.+)", re.IGNORECASE),
    "diag_agent_ce":     re.compile(r"Agent/CE\s*[:：]\s*(.+)", re.IGNORECASE),
    "diag_model":        re.compile(r"MODEL\s*[:：]\s*(.+)", re.IGNORECASE),
    "diag_warranty":     re.compile(r"WARRANTY STATUS\s*[:：]\s*(.+)", re.IGNORECASE),
    "diag_problem":      re.compile(r"PROBLEM DESCRIPTION\s*[:：]\s*(.+)", re.IGNORECASE),
    "diag_esc_approval": re.compile(r"ESC Approval\s*[:：]\s*(.+)", re.IGNORECASE),
}

_PARTS_BLOCK = re.compile(
    r"=+\s*PART LIST REQUEST.*?=+\s*\n(.*?)(?:\n=+|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_diagnose(text: str | None) -> dict:
    if not text:
        return {k: None for k in list(_DIAG_PATTERNS.keys()) + ["diag_parts_request"]}
    result = {}
    for key, pat in _DIAG_PATTERNS.items():
        m = pat.search(text)
        result[key] = m.group(1).strip() if m else None
    m = _PARTS_BLOCK.search(text)
    result["diag_parts_request"] = m.group(1).strip() if m else None
    return result


def _derive_work_order_type(item: dict, fetched_value: str | None) -> str | None:
    group_title = (item.get("group") or {}).get("title") or ""
    if group_title.upper().endswith(" CCI"):
        return "CCI"
    if group_title.upper().endswith(" ONSITE"):
        return "ONS"
    return fetched_value


def item_to_row(item: dict, board_id: str, asp_board: str,
                col_map: dict[str, str] | None = None) -> dict:
    cvs = item.get("column_values", [])
    cm  = col_map or {}
    status_text,  _ = _cv_by_title(cvs, cm, "Status")
    wotype_text,  _ = _cv_by_title(cvs, cm, "Work Order Type")
    wo_text,      _ = _cv_by_title(cvs, cm, "WO# / Case ID#")
    serial_text,  _ = _cv_by_title(cvs, cm, "Serial Number")
    diag_text,    _ = _cv_by_title(cvs, cm, "Diagnose note")
    repair_text,  _ = _cv_by_title(cvs, cm, "Repair Note")
    ppsn_text,    _ = _cv_by_title(cvs, cm, "PPSN Category")
    rrr_text,     _ = _cv_by_title(cvs, cm, "RRR Category")
    loc_text,     _ = _cv_by_title(cvs, cm, "Location")
    creator = item.get("creator") or {}
    parsed  = parse_diagnose(diag_text)
    creator_id = str(creator.get("id")) if creator.get("id") else None
    return {
        "db_synced_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "item_updated_at": item.get("updated_at"),
        "board_id":        board_id,
        "asp_board":       asp_board,
        "monday_item_id":  item["id"],
        "item_name":       item.get("name"),
        "item_created_at": item.get("created_at"),
        "status":          status_text,
        "work_order_type": _derive_work_order_type(item, wotype_text),
        "wo_case_id":      wo_text,
        "serial_number":   serial_text,
        "diagnose_note":   diag_text,
        "repair_note":     repair_text,
        "ppsn_category":   ppsn_text,
        "rrr_category":    rrr_text,
        "location":        loc_text,
        "creator_id":      creator_id,
        "_creator_name":   creator.get("name"),
        "_creator_email":  creator.get("email"),
        **parsed,
    }


UPSERT_CREATOR_SQL = """
INSERT OR IGNORE INTO creators (creator_id, creator_name, creator_email)
VALUES (:creator_id, :creator_name, :creator_email)
"""

UPSERT_SQL = """
INSERT INTO technical_escalation (
    db_synced_at, item_updated_at, board_id, asp_board,
    monday_item_id, item_name, item_created_at,
    status, work_order_type, wo_case_id, serial_number,
    diagnose_note, repair_note, ppsn_category, rrr_category, location,
    creator_id,
    diag_datetime, diag_agent_ce, diag_model, diag_warranty, diag_problem,
    diag_esc_approval, diag_parts_request
) VALUES (
    :db_synced_at, :item_updated_at, :board_id, :asp_board,
    :monday_item_id, :item_name, :item_created_at,
    :status, :work_order_type, :wo_case_id, :serial_number,
    :diagnose_note, :repair_note, :ppsn_category, :rrr_category, :location,
    :creator_id,
    :diag_datetime, :diag_agent_ce, :diag_model, :diag_warranty, :diag_problem,
    :diag_esc_approval, :diag_parts_request
)
ON CONFLICT(monday_item_id) DO UPDATE SET
    db_synced_at       = excluded.db_synced_at,
    item_updated_at    = excluded.item_updated_at,
    board_id           = COALESCE(excluded.board_id,        board_id),
    asp_board          = COALESCE(excluded.asp_board,       asp_board),
    item_name          = COALESCE(excluded.item_name,       item_name),
    item_created_at    = COALESCE(excluded.item_created_at, item_created_at),
    status             = COALESCE(excluded.status,          status),
    work_order_type    = COALESCE(excluded.work_order_type, work_order_type),
    wo_case_id         = COALESCE(excluded.wo_case_id,      wo_case_id),
    serial_number      = COALESCE(excluded.serial_number,   serial_number),
    diagnose_note      = COALESCE(excluded.diagnose_note,   diagnose_note),
    repair_note        = COALESCE(excluded.repair_note,     repair_note),
    ppsn_category      = COALESCE(excluded.ppsn_category,   ppsn_category),
    rrr_category       = COALESCE(excluded.rrr_category,    rrr_category),
    location           = COALESCE(excluded.location,        location),
    creator_id         = COALESCE(excluded.creator_id,      creator_id),
    diag_datetime      = COALESCE(excluded.diag_datetime,   diag_datetime),
    diag_agent_ce      = COALESCE(excluded.diag_agent_ce,   diag_agent_ce),
    diag_model         = COALESCE(excluded.diag_model,      diag_model),
    diag_warranty      = COALESCE(excluded.diag_warranty,   diag_warranty),
    diag_problem       = COALESCE(excluded.diag_problem,    diag_problem),
    diag_esc_approval  = COALESCE(excluded.diag_esc_approval, diag_esc_approval),
    diag_parts_request = COALESCE(excluded.diag_parts_request, diag_parts_request)
"""


# ─── WO Order Type auto-fill from asp_details ────────────────────────────────

_APPLY_WO_TYPE_CCI_SQL = """
UPDATE technical_escalation
SET    work_order_type = 'CCI'
WHERE  (work_order_type IS NULL OR work_order_type = '')
AND    board_id IN (
           SELECT monday_board_id
           FROM   asp_details
           WHERE  LOWER(TRIM(operation_support)) = 'cci only'
       )
"""


def _apply_wo_type_from_asp(conn: sqlite3.Connection) -> int:
    """
    Fill empty work_order_type with 'CCI' for any technical_escalation row
    whose board matches an asp_details entry with operation_support = 'CCI Only'.

    Safe to call repeatedly — only touches rows where work_order_type is NULL
    or blank, so it never overwrites an already-set value.

    Returns the number of rows updated.
    """
    try:
        cur = conn.execute(_APPLY_WO_TYPE_CCI_SQL)
        conn.commit()
        return cur.rowcount
    except Exception as exc:
        log.warning("_apply_wo_type_from_asp: %s", exc)
        return 0


def upsert_items(conn: sqlite3.Connection, items: list[dict],
                 board_id: str, asp_board: str,
                 col_map: dict[str, str] | None = None) -> int:
    rows = [item_to_row(i, board_id, asp_board, col_map) for i in items]
    conn.executemany(UPSERT_CREATOR_SQL, [
        {"creator_id": r["creator_id"], "creator_name": r["_creator_name"],
         "creator_email": r["_creator_email"]}
        for r in rows if r["creator_id"]
    ])
    conn.executemany(UPSERT_SQL, rows)
    conn.commit()
    filled = _apply_wo_type_from_asp(conn)
    if filled:
        log.info("work_order_type auto-fill: set 'CCI' on %d row(s) via asp_details", filled)
    return len(rows)


# ─── Updates ─────────────────────────────────────────────────────────────────

_TAG_RE = _re.compile(r"<[^>]+>")


def _strip_html(raw: str | None) -> str | None:
    if not raw:
        return raw
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return " ".join(text.split())


def fetch_item_updates(item_id: str, limit: int = 50) -> list[dict]:
    data = gql(UPDATES_QUERY, {"itemId": item_id, "limit": limit})
    items = data["data"]["items"]
    if not items:
        return []
    return items[0].get("updates", [])


UPSERT_UPDATE_SQL = """
INSERT INTO item_updates
    (update_id, monday_item_id, body_text, created_at, updated_at, creator_id)
VALUES (:update_id, :monday_item_id, :body_text, :created_at, :updated_at, :creator_id)
ON CONFLICT(update_id) DO UPDATE SET
    body_text  = COALESCE(excluded.body_text,  body_text),
    updated_at = COALESCE(excluded.updated_at, updated_at),
    creator_id = COALESCE(excluded.creator_id, creator_id)
"""

UPSERT_REPLY_SQL = """
INSERT INTO item_update_replies
    (reply_id, update_id, body_text, created_at, creator_id)
VALUES (:reply_id, :update_id, :body_text, :created_at, :creator_id)
ON CONFLICT(reply_id) DO UPDATE SET
    body_text  = COALESCE(excluded.body_text,  body_text),
    creator_id = COALESCE(excluded.creator_id, creator_id)
"""


def upsert_updates(conn: sqlite3.Connection, monday_item_id: str, updates: list[dict]) -> int:
    if not updates:
        return 0
    creators = {}
    for u in updates:
        c = u.get("creator") or {}
        if c.get("id"):
            cid = str(c["id"])
            creators[cid] = {"creator_id": cid, "creator_name": c.get("name"),
                             "creator_email": c.get("email")}
        for r in u.get("replies", []):
            rc = r.get("creator") or {}
            if rc.get("id"):
                cid = str(rc["id"])
                creators[cid] = {"creator_id": cid, "creator_name": rc.get("name"),
                                 "creator_email": rc.get("email")}
    conn.executemany(UPSERT_CREATOR_SQL, list(creators.values()))
    update_rows, reply_rows = [], []
    for u in updates:
        c   = u.get("creator") or {}
        cid = str(c["id"]) if c.get("id") else None
        update_rows.append({
            "update_id": u["id"], "monday_item_id": monday_item_id,
            "body_text": _strip_html(u.get("body")), "created_at": u.get("created_at"),
            "updated_at": u.get("updated_at"), "creator_id": cid,
        })
        for r in u.get("replies", []):
            rc   = r.get("creator") or {}
            rcid = str(rc["id"]) if rc.get("id") else None
            reply_rows.append({
                "reply_id": r["id"], "update_id": u["id"],
                "body_text": _strip_html(r.get("body")), "created_at": r.get("created_at"),
                "creator_id": rcid,
            })
    conn.executemany(UPSERT_UPDATE_SQL, update_rows)
    conn.executemany(UPSERT_REPLY_SQL, reply_rows)
    conn.commit()
    return len(update_rows) + len(reply_rows)


# ─── Sync logic ───────────────────────────────────────────────────────────────

def run_sync_board(conn: sqlite3.Connection, state: dict, board: dict,
                   force_full: bool = False, stop_event=None) -> None:
    board_id  = board["board_id"]
    asp_board = board["asp_board"]
    t0        = time.monotonic()

    board_state = get_board_state(state, board_id)
    if force_full:
        board_state["last_sync"] = None

    last_sync = board_state.get("last_sync")
    run_type  = "incremental" if last_sync else "full"

    log.info("Mode:\t%s", run_type.capitalize())

    run_started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    items, col_map, group_summaries = fetch_all_items(board_id, since=last_sync, conn=conn)
    upserted = upsert_items(conn, items, board_id, asp_board, col_map) if items else 0

    total_updates = 0
    total_items   = len(items)
    for idx, item in enumerate(items, 1):
        if stop_event and stop_event.is_set():
            log.info("Stop requested — aborting at item %d/%d", idx, total_items)
            break
        updates = fetch_item_updates(item["id"])
        n = upsert_updates(conn, item["id"], updates)
        total_updates += n
    duration = time.monotonic() - t0
    rows_per_group = upserted if len(group_summaries) == 1 else 0
    for group_summary in group_summaries:
        log.info("  Groups:\t%s", group_summary["title"])
        log.info("  Done:\t%.1fs\titems:\t%d\trows:\t%d",
                 duration, group_summary["items"], rows_per_group)

    if items:
        log.info("[%s]\tUpdates:\t%d across %d item(s)", asp_board, total_updates, total_items)

    # Apply CCI work_order_type fill for any rows that still have it blank
    # (covers rows inserted before asp_details was linked, or re-syncs).
    filled = _apply_wo_type_from_asp(conn)
    if filled:
        log.info("[%s]\twork_order_type CCI fill:\t%d row(s) updated", asp_board, filled)

    conn.execute(
        "INSERT INTO sync_log "
        "(run_at, board_id, asp_board, run_type, items_found, items_upserted, duration_sec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_started_at, board_id, asp_board, run_type, len(items), upserted, round(duration, 2)),
    )
    conn.commit()
    conn.execute("DELETE FROM sync_log WHERE run_at < datetime('now', '-4 months')")
    conn.commit()

    board_state["last_sync"]    = run_started_at
    board_state["total_synced"] = board_state.get("total_synced", 0) + upserted
    save_state(state)


def run_sync_all(conn: sqlite3.Connection, state: dict, boards: list[dict],
                 force_full: bool = False, stop_event=None) -> None:
    log.info("Starting sync for %d board(s)", len(boards))
    for idx, board in enumerate(boards, 1):
        if stop_event and stop_event.is_set():
            log.info("Stop requested — aborting before board %d (%s)", idx, board["asp_board"])
            break
        log.info("--------------------------------------------")
        log.info("[%d/%d]\tBoard:\t%s", idx, len(boards), board["asp_board"])
        try:
            run_sync_board(conn, state, board, force_full=force_full, stop_event=stop_event)
        except Exception as exc:
            log.error("Board '%s' failed: %s — skipping.", board["asp_board"], exc)
    log.info("All boards completed")


def _backfill_updates(conn: sqlite3.Connection, stop_event=None) -> None:
    rows = conn.execute(
        "SELECT monday_item_id FROM technical_escalation "
        "WHERE monday_item_id IS NOT NULL ORDER BY item_updated_at DESC"
    ).fetchall()
    total, done, skipped = len(rows), 0, 0
    log.info("Backfill started: %d items (~%d min at 1 req/sec)", total, total // 60 + 1)
    for row in rows:
        if stop_event and stop_event.is_set():
            log.info("Stop requested — aborting backfill at item %d/%d", done + 1, total)
            break
        try:
            updates = fetch_item_updates(row["monday_item_id"])
            n = upsert_updates(conn, row["monday_item_id"], updates)
            done += 1
            log.info("  [%d/%d] %s — %d posts/replies", done, total, row["monday_item_id"], n)
        except Exception as exc:
            log.warning("  [%d/%d] %s — failed: %s", done + 1, total, row["monday_item_id"], exc)
            skipped += 1
        time.sleep(1)
    log.info("Backfill complete: %d processed, %d skipped.", done, skipped)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Monday.com → SQLite smart poller (multi-board)")
    parser.add_argument("--once",             action="store_true")
    parser.add_argument("--full",             action="store_true")
    parser.add_argument("--summary",          action="store_true")
    parser.add_argument("--backfill-updates", action="store_true")
    parser.add_argument("--board",            type=str, default=None)
    parser.add_argument("--interval",         type=int, default=POLL_INTERVAL)
    parser.add_argument("--import-xlsx",      type=str, default=None, metavar="XLSX_FILE")
    args = parser.parse_args()

    conn   = get_db()
    state  = load_state()
    boards = load_boards()

    if args.import_xlsx:
        new_boards = [b for b in load_boards(args.import_xlsx) if b["board_id"] not in state["boards"]]
        if new_boards:
            run_sync_all(conn, state, new_boards, force_full=True)
        conn.close()
        return

    if args.board:
        boards = [b for b in boards if b["board_id"] == str(args.board)]
        if not boards:
            log.error("Board ID '%s' not found in %s", args.board, XLSX_FILE)
            return

    if args.summary:
        from pprint import pprint
        total = conn.execute("SELECT COUNT(*) FROM technical_escalation").fetchone()[0]
        print(f"Total items: {total}")
        return

    if args.backfill_updates:
        _backfill_updates(conn)
        return

    if args.once or args.full:
        run_sync_all(conn, state, boards, force_full=args.full)
        return

    log.info("Poller started. Boards=%d  Interval=%ds. Ctrl+C to stop.", len(boards), args.interval)
    try:
        while True:
            run_sync_all(conn, state, boards)
            log.info("Next poll in %d seconds…", args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Poller stopped by user.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
