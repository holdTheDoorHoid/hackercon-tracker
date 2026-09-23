"""SQLite storage for Hackercon Tracker.

One file on disk (data/tracker.db). Plain sqlite3, no ORM, so the schema is
easy to read and back up: copy the .db file and you have everything.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("CONTRACKER_DB", DATA_DIR / "tracker.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conferences (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    aliases TEXT DEFAULT '[]',            -- JSON list of other spellings
    type TEXT DEFAULT 'hacker',           -- bsides | hacker | maker | hardware | industry | academic | camp | online
    website TEXT,
    city TEXT,
    region TEXT,                          -- state / province
    country TEXT,                         -- ISO-2 (US, CA, GB ...)
    lat REAL,
    lon REAL,
    online INTEGER DEFAULT 0,
    typical_month INTEGER,                -- 1-12, when it usually happens
    typical_pattern TEXT,                 -- e.g. "2nd weekend of May"
    attendance_est INTEGER,               -- best single number
    attendance_band TEXT,                 -- the spreadsheet's band text
    description TEXT,
    contact_email TEXT,
    cfp_email TEXT,
    sponsor_email TEXT,
    discord_url TEXT,
    twitter TEXT,
    mastodon TEXT,
    bluesky TEXT,
    linkedin TEXT,
    instagram TEXT,
    youtube TEXT,
    cfp_url TEXT,
    cfv_url TEXT,                         -- call for villages / workshops / trainings
    sponsor_url TEXT,
    sponsor_docs_url TEXT,
    vending_policy TEXT DEFAULT 'unknown',    -- yes | sponsor-only | no | unknown
    vending_notes TEXT,
    table_cost TEXT,
    workshop_track TEXT DEFAULT 'unknown',    -- yes | no | unknown
    village_hosting TEXT DEFAULT 'unknown',   -- yes | no | unknown
    iot_village_partner INTEGER DEFAULT 0,    -- the "IOT Vill" column
    ticket_price TEXT,
    relationship_notes TEXT,
    past_rating INTEGER,                  -- 1-5 "worth it" from last time
    past_sales REAL,
    past_notes TEXT,
    priority TEXT DEFAULT 'normal',       -- high | normal | low | ignore
    archived INTEGER DEFAULT 0,
    source TEXT,
    last_verified TEXT,
    verification_notes TEXT,
    notes TEXT,
    interval_years INTEGER,
    date_hints TEXT,
    hints_checked TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS editions (
    id INTEGER PRIMARY KEY,
    conference_id INTEGER NOT NULL REFERENCES conferences(id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    label TEXT,                           -- "BSidesNEPA 2025"
    start_date TEXT,                      -- ISO date
    end_date TEXT,
    training_start TEXT,
    training_end TEXT,
    dates_status TEXT DEFAULT 'unknown',  -- confirmed | announced | tentative | estimated | unknown | cancelled | postponed
    venue TEXT,
    city TEXT,
    url TEXT,
    attendance_actual INTEGER,
    our_status TEXT DEFAULT 'considering',-- considering | applied | accepted | attending | attended | declined | rejected | skipped | not-going
    hotel TEXT,
    travel TEXT,
    pto TEXT,
    budget_est REAL,
    sales REAL,
    leads INTEGER,
    worth_it INTEGER,                     -- 1-5 after-action rating
    aar_notes TEXT,
    notes TEXT,
    source TEXT,
    last_verified TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(conference_id, year)
);

CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                   -- cfp | training | cfv | vendor | sponsor | hotel | travel | early_bird | custom
    label TEXT,
    due_date TEXT,                        -- ISO date, may be NULL when only status is known
    status TEXT DEFAULT 'open',           -- open | submitted | accepted | rejected | declined | closed | done | na | waitlist
    url TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    conference_id INTEGER NOT NULL REFERENCES conferences(id) ON DELETE CASCADE,
    name TEXT,
    role TEXT,
    email TEXT,
    phone TEXT,
    notes TEXT,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    conference_id INTEGER NOT NULL REFERENCES conferences(id) ON DELETE CASCADE,
    edition_id INTEGER REFERENCES editions(id) ON DELETE SET NULL,
    date TEXT NOT NULL,
    channel TEXT DEFAULT 'gmail',         -- gmail | web-form | meeting | discord | phone | other
    direction TEXT DEFAULT 'out',         -- out | in
    subject TEXT,
    summary TEXT,
    gmail_thread_id TEXT,
    gmail_message_id TEXT,
    follow_up_due TEXT,
    status TEXT DEFAULT 'sent',           -- sent | replied | no-reply | meeting | closed
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    gmail_id TEXT UNIQUE NOT NULL,
    thread_id TEXT,
    conference_id INTEGER REFERENCES conferences(id) ON DELETE SET NULL,
    date TEXT,
    from_addr TEXT,
    to_addr TEXT,
    subject TEXT,
    snippet TEXT,
    body_text TEXT,
    labels TEXT DEFAULT '[]',
    is_sent INTEGER DEFAULT 0,
    is_unread INTEGER DEFAULT 0,
    match_reason TEXT,
    synced_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_emails_conf ON emails(conference_id);
CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT,
    body TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT,
    url TEXT,
    notes TEXT,
    last_checked TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT (datetime('now')),
    conference_id INTEGER,
    kind TEXT,
    message TEXT
);
"""

DEFAULT_SETTINGS = {
    "home_label": "",                # set on the Settings page; scores need it
    "home_lat": "",
    "home_lon": "",
    "drive_max_hours": "6",          # beyond this we assume a flight
    "avg_mph": "58",
    "road_factor": "1.22",           # straight-line -> road distance fudge
    "followup_days": "14",
    "hotel_lead_days": "60",
    "flight_lead_days": "45",
    "weight_size": "25",
    "weight_travel": "20",
    "weight_vending": "20",
    "weight_workshop": "15",
    "weight_pto": "10",
    "weight_past": "10",
    "gmail_label_prefix": "Cons",
    "gmail_sync_query": "newer_than:2y",
    "owner_name": "",
    "org_name": "",
    "auto_refresh_days": "7",        # pull the public feeds this often; 0 = never
    "refresh_hints": "1",            # also read each unconfirmed conference's website for date hints
    "last_refresh": "",
    "seed_applied": "",              # "generated" stamp of the bundled dataset last merged in
    "suppressed_slugs": "[]",        # conferences you deleted; updates will not bring them back
}

MIGRATIONS = [  # (table, column, type) added after the first release
    ("conferences", "interval_years", "INTEGER"),
    ("conferences", "date_hints", "TEXT"),
    ("conferences", "hints_checked", "TEXT"),
]


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, detect_types=0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        con.commit()
    finally:
        con.close()


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------- small helpers ----------

def rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def row(con, sql, params=()):
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_settings(con) -> dict:
    s = dict(DEFAULT_SETTINGS)
    for r in con.execute("SELECT key, value FROM settings"):
        s[r["key"]] = r["value"]
    return s


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def log_activity(con, kind, message, conference_id=None):
    con.execute("INSERT INTO activity(kind, message, conference_id) VALUES (?, ?, ?)", (kind, message, conference_id))


def update_row(con, table, id_, fields: dict):
    fields = {k: v for k, v in fields.items() if k not in ("id", "created_at")}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    has_updated = table in ("conferences", "editions", "deadlines")
    if has_updated:
        sets += ", updated_at=datetime('now')"
    con.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), id_))


def insert_row(con, table, fields: dict) -> int:
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    cur = con.execute(f"INSERT INTO {table}({cols}) VALUES ({qs})", tuple(fields.values()))
    return cur.lastrowid


def parse_date(v) -> str | None:
    """Return ISO date string or None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def jloads(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except Exception:
        return default if default is not None else []
