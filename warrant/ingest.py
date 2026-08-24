"""
Webhook ingest: verify, deduplicate, persist. Nothing else.

Two rules this module exists to enforce:

1. The HMAC is computed over the RAW request bytes. Never over a re-serialised
   parse of the body -- json.dumps(json.loads(x)) is not byte-identical to x,
   and the signature will never match.

2. Duplicate delivery is normal, not an error. Razorpay documents that the same
   event may arrive more than once. Dedup is enforced by a UNIQUE constraint in
   the database, not by SELECT-then-INSERT, which races under concurrent
   delivery.

No decision logic lives here.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

# The header Razorpay sends carrying the HMAC of the request body.
SIGNATURE_HEADER = "x-razorpay-signature"

# The header documented as unique per event; our deduplication key.
# UNVERIFIED against a live test-mode payload -- poc/payloads/ is still empty.
EVENT_ID_HEADER = "x-razorpay-event-id"


class IngestResult(str, Enum):
    STORED = "stored"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class IngestOutcome:
    result: IngestResult
    event_id: str
    event_type: str | None


class SignatureError(Exception):
    """Raised when the HMAC does not match the raw body."""


class MissingEventIdError(Exception):
    """Raised when the delivery carries no event id and cannot be deduplicated."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def compute_signature(raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the raw body, hex-encoded, keyed by the webhook secret."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, provided_signature: str, secret: str) -> bool:
    """Constant-time comparison. Takes bytes; refuses to be handed a parsed dict."""
    if not isinstance(raw_body, (bytes, bytearray)):
        raise TypeError("raw_body must be bytes -- never a parsed or re-serialised body")
    if not provided_signature:
        return False
    return hmac.compare_digest(compute_signature(bytes(raw_body), secret), provided_signature)


def _extract_event_type(raw_body: bytes) -> str | None:
    """Best-effort read of the `event` field. Only called AFTER verification.

    Returns None on malformed JSON rather than raising: the raw bytes are
    already stored, and a body we cannot parse is still evidence worth keeping.
    """
    try:
        return json.loads(raw_body).get("event")
    except Exception:
        return None


def _audit(conn: sqlite3.Connection, action: str, event_id: str | None, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, event_id, detail_json) VALUES (?,?,?,?,?)",
        (_now(), "ingest", action, event_id, json.dumps(detail, sort_keys=True)),
    )


def ingest_event(
    conn: sqlite3.Connection,
    raw_body: bytes,
    headers: dict[str, str],
    secret: str,
) -> IngestOutcome:
    """
    Verify, deduplicate and persist one webhook delivery.

    Raises SignatureError or MissingEventIdError. On success returns STORED for
    a first delivery and DUPLICATE for any repeat -- both are non-error outcomes.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    signature = lower.get(SIGNATURE_HEADER, "")

    if not verify_signature(raw_body, signature, secret):
        _audit(conn, "signature_rejected", lower.get(EVENT_ID_HEADER), {
            "reason": "hmac_mismatch", "body_bytes": len(raw_body),
        })
        conn.commit()
        raise SignatureError("signature does not match raw body")

    event_id = lower.get(EVENT_ID_HEADER)
    if not event_id:
        _audit(conn, "missing_event_id", None, {"reason": "no_dedup_key"})
        conn.commit()
        raise MissingEventIdError(f"delivery carried no {EVENT_ID_HEADER} header")

    event_type = _extract_event_type(raw_body)

    # Dedup is the UNIQUE constraint doing the work. A SELECT-then-INSERT would
    # race with a concurrent redelivery of the same event.
    cur = conn.execute(
        """INSERT INTO events (event_id, event_type, raw_body, headers_json, signature, received_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(event_id) DO NOTHING""",
        (event_id, event_type, sqlite3.Binary(raw_body),
         json.dumps(lower, sort_keys=True), signature, _now()),
    )

    if cur.rowcount == 1:
        _audit(conn, "event_stored", event_id, {"event_type": event_type})
        outcome = IngestResult.STORED
    else:
        # Audited, not discarded. The duplicate count is evidence.
        _audit(conn, "duplicate_suppressed", event_id, {"event_type": event_type})
        outcome = IngestResult.DUPLICATE

    conn.commit()
    return IngestOutcome(result=outcome, event_id=event_id, event_type=event_type)
