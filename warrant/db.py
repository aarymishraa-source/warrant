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

-- One row per failed payment we are tracking. `version` is the optimistic
-- concurrency guard: webhooks are unordered, so two deliveries for the same
-- case can be in flight at once. See warrant/core.py.
CREATE TABLE IF NOT EXISTS cases (
    case_id             TEXT    PRIMARY KEY,
    payment_id          TEXT,
    order_id            TEXT,
    customer_id         TEXT    NOT NULL,
    case_type           TEXT    NOT NULL,
    state               TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    ticket_amount_paise INTEGER NOT NULL,
    created_at          TEXT,
    updated_at          TEXT
);

-- APPEND-ONLY. Nothing in this codebase UPDATEs or DELETEs this table. A wrong
-- transition is corrected by appending another one, so the Day 10 ledger can
-- reconstruct what we believed and when.
CREATE TABLE IF NOT EXISTS case_transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT,
    event_id   TEXT,
    at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_case ON case_transitions(case_id);

-- One row per case, written once, before any model or heuristic reads the case.
CREATE TABLE IF NOT EXISTS assignments (
    case_id      TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    arm          TEXT NOT NULL,
    carved_out   INTEGER NOT NULL,
    carve_reason TEXT,
    assigned_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_customer ON assignments(customer_id);

-- The intent ledger. Written BEFORE the external call, never after: if the
-- process dies mid-call the intent survives and reconciliation can find it.
-- `idempotency_key` is OUR key and is UNIQUE here, which is what makes a
-- duplicate execution impossible to insert rather than merely unlikely.
-- See warrant/act.py.
CREATE TABLE IF NOT EXISTS intents (
    intent_id         TEXT    PRIMARY KEY,
    case_id           TEXT    NOT NULL,
    idempotency_key   TEXT    NOT NULL UNIQUE,
    action_type       TEXT    NOT NULL,
    action_cost_paise INTEGER NOT NULL,
    status            TEXT    NOT NULL,
    provider_ref      TEXT,
    created_at        TEXT    NOT NULL,
    resolved_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_intents_case ON intents(case_id);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status);
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
