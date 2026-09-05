"""
Intent ledger queries for the HTTP reconciliation endpoint.

This module complements warrant.act by providing read-only queries over the
append-only intent ledger that are useful for the HTTP layer.

Key function: get_pending_intents(conn, before_minutes=30)
  Returns all UNKNOWN/PENDING intents older than the threshold.
  This is what drives the batch reconciliation loop: poll this endpoint,
  query the external provider for each, then call resolve() to record truth.

Design constraints
------------------
- Append-only: this module NEVER UPDATEs or DELETEs intent rows.
- Stats are read-only aggregates; resolve() only sets is_reconciled=1 and
  records the provider ref -- it does not change status, which is the
  actuator's job.

Relationship to act.py
-----------------------
- act.py owns the intent write path: execute(), retry(), cancel_pending().
- This module owns the read/reconcile path: listing, batching, and the
  is_reconciled flag that prevents re-processing already-resolved intents.
- Both modules share the same sqlite3 Connection from the request scope.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from warrant.act import IntentStatus


# ------------------------------------------------------------------------- data shape

@dataclass
class IntentSummary:
    """Lightweight intent record for the HTTP reconciliation endpoint."""
    intent_id: str
    case_id: str
    order_id: str | None
    status: IntentStatus
    action_type: str
    attempt: int
    external_ref: str | None
    provider: str
    created_at: str        # ISO-8601
    is_reconciled: bool


# ------------------------------------------------------------------------- reconciliation queries

def get_pending_intents(
    conn: sqlite3.Connection,
    before_minutes: int = 30,
) -> list[IntentSummary]:
    """Return UNKNOWN/PENDING intents older than before_minutes.

    These are the candidates for batch reconciliation. Call the external
    provider to determine their true final status, then record it via resolve().
    """
    cutoff = _utc_now_minus(minutes=before_minutes)
    rows = conn.execute("""
        SELECT
            i.intent_id,
            i.case_id,
            c.order_id,
            i.status,
            i.action_type,
            i.provider_ref,
            i.created_at
        FROM intents i
        JOIN cases c ON c.case_id = i.case_id
        WHERE i.status IN (?, ?)
          AND i.created_at < ?
        ORDER BY i.created_at ASC
    """, (
        IntentStatus.UNKNOWN.value,
        IntentStatus.PENDING.value,
        cutoff,
    )).fetchall()

    results = []
    for row in rows:
        # Derive attempt number from intent_id if possible; default to 1
        attempt = _attempt_from_id(row["intent_id"])
        results.append(IntentSummary(
            intent_id=row["intent_id"],
            case_id=row["case_id"],
            order_id=row["order_id"],
            status=IntentStatus(row["status"]),
            action_type=row["action_type"],
            attempt=attempt,
            external_ref=row["provider_ref"],
            provider="razorpay",   # only razorpay for now
            created_at=row["created_at"],
            is_reconciled=False,
        ))
    return results


def get_intents_for_case(conn: sqlite3.Connection, case_id: str) -> list[IntentSummary]:
    """Return all intents for a case, newest first."""
    rows = conn.execute("""
        SELECT
            i.intent_id,
            i.case_id,
            c.order_id,
            i.status,
            i.action_type,
            i.provider_ref,
            i.created_at
        FROM intents i
        JOIN cases c ON c.case_id = i.case_id
        WHERE i.case_id = ?
        ORDER BY i.created_at DESC
    """, (case_id,)).fetchall()

    results = []
    for row in rows:
        attempt = _attempt_from_id(row["intent_id"])
        is_reconciled = row["provider_ref"] is not None
        results.append(IntentSummary(
            intent_id=row["intent_id"],
            case_id=row["case_id"],
            order_id=row["order_id"],
            status=IntentStatus(row["status"]),
            action_type=row["action_type"],
            attempt=attempt,
            external_ref=row["provider_ref"],
            provider="razorpay",
            created_at=row["created_at"],
            is_reconciled=is_reconciled,
        ))
    return results


def resolve(
    conn: sqlite3.Connection,
    intent_id: str,
    external_ref: str,
) -> bool:
    """Record a confirmed provider reference for an intent.

    Sets the provider_ref field, which is what signals that the intent has
    been reconciled (the external provider confirmed the action's outcome).

    Returns True if the intent was found and updated, False if not found.
    Only PENDING and UNKNOWN intents are updated.
    """
    cur = conn.execute("""
        UPDATE intents
        SET provider_ref = ?
        WHERE intent_id = ?
          AND status IN (?, ?)
    """, (
        external_ref,
        intent_id,
        IntentStatus.PENDING.value,
        IntentStatus.UNKNOWN.value,
    ))
    conn.commit()
    return cur.rowcount > 0


def stats(conn: sqlite3.Connection) -> dict:
    """Return intent ledger statistics for the dashboard /health endpoint."""
    row = conn.execute("""
        SELECT
            COUNT(*)                           AS total,
            COALESCE(SUM(status = 'EXECUTED'),  0) AS executed,
            COALESCE(SUM(status = 'FAILED'),    0) AS failed,
            COALESCE(SUM(status = 'UNKNOWN'),   0) AS unknown,
            COALESCE(SUM(status = 'PENDING'),  0) AS pending,
            COALESCE(SUM(status = 'CANCELLED'),0) AS cancelled,
            COALESCE(SUM(provider_ref IS NOT NULL
                      AND status NOT IN ('CANCELLED')), 0) AS reconciled,
            COUNT(DISTINCT case_id)             AS cases_touched
        FROM intents
    """).fetchone()
    return dict(row) if row else {}


# ------------------------------------------------------------------------- internal helpers

def _attempt_from_id(intent_id: str) -> int:
    """Derive attempt number from intent_id.

    Intent IDs are generated as: SHA256(case_id || '-' || str(attempt))[0..16]
    We don't store attempt number in the intent row, so this is a heuristic.
    Default to 1 when the intent_id format is unrecognised.
    """
    # The intent_id is a hex string; if it looks like a hash, try to extract
    # any trailing digit as the attempt. This is approximate.
    if intent_id and intent_id[-1].isdigit():
        try:
            return int(intent_id[-1])
        except ValueError:
            pass
    return 1


def _utc_now_minus(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
