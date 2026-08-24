"""
SQLite access for Warrant.

Deliberately thin: no ORM, no migration framework. The schema is small enough
that `CREATE TABLE IF NOT EXISTS` is the whole migration story for a 12-day
build. If the schema ever needs to change destructively, delete the file and
re-run; there is no production data.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "warrant.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT    NOT NULL UNIQUE,
    event_type   TEXT,
    raw_body     BLOB    NOT NULL,
    headers_json TEXT    NOT NULL,
    signature    TEXT,
    received_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    event_id    TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_id);
"""


def db_path() -> str:
    return os.environ.get("WARRANT_DB", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with FK enforcement and Row access."""
    target = path or db_path()
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
