"""
Day 3 ingest tests. Deterministic fixtures only -- nothing here touches the
network or a real Razorpay account.
"""
from __future__ import annotations

import json

import pytest

from warrant import db
from warrant.ingest import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    IngestResult,
    MissingEventIdError,
    SignatureError,
    compute_signature,
    ingest_event,
    verify_signature,
)

SECRET = "test_webhook_secret_do_not_use"

# Shaped like a Razorpay payment.failed body. Field names taken from Razorpay's
# published webhook payload reference. Values are invented test fixtures.
RAW_BODY = json.dumps({
    "entity": "event",
    "event": "payment.failed",
    "contains": ["payment"],
    "payload": {"payment": {"entity": {
        "id": "pay_TESTFIXTURE001",
        "order_id": "order_TESTFIXTURE001",
        "status": "failed",
        "method": "card",
        "amount": 34000,
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment failed",
        "error_source": "customer",
        "error_step": "payment_authentication",
        "error_reason": "payment_failed",
    }}},
    "created_at": 1756000000,
}, separators=(",", ":")).encode()


def headers(raw: bytes = RAW_BODY, event_id: str = "evt_TESTFIXTURE001",
            secret: str = SECRET, signature: str | None = None) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature if signature is not None else compute_signature(raw, secret),
        EVENT_ID_HEADER: event_id,
        "user-agent": "Razorpay-Webhook",
    }


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ----------------------------------------------------------------- signature

def test_valid_signature_accepted(conn):
    out = ingest_event(conn, RAW_BODY, headers(), SECRET)
    assert out.result is IngestResult.STORED
    assert out.event_type == "payment.failed"


def test_invalid_signature_rejected(conn):
    with pytest.raises(SignatureError):
        ingest_event(conn, RAW_BODY, headers(signature="deadbeef"), SECRET)
    assert count(conn, "events") == 0


def test_modified_raw_body_rejected(conn):
    """Signature computed over the original body must not validate a tampered one."""
    good = headers()
    tampered = RAW_BODY.replace(b'"amount":34000', b'"amount":99900')
    assert tampered != RAW_BODY
    with pytest.raises(SignatureError):
        ingest_event(conn, tampered, good, SECRET)
    assert count(conn, "events") == 0


def test_reserialised_body_would_not_verify():
    """Guards the classic bug: json.dumps(json.loads(x)) != x, byte-wise."""
    sig = compute_signature(RAW_BODY, SECRET)
    reserialised = json.dumps(json.loads(RAW_BODY)).encode()
    assert reserialised != RAW_BODY
    assert not verify_signature(reserialised, sig, SECRET)


def test_verify_refuses_non_bytes():
    with pytest.raises(TypeError):
        verify_signature(json.loads(RAW_BODY), "sig", SECRET)  # type: ignore[arg-type]


def test_rejected_signature_is_audited(conn):
    with pytest.raises(SignatureError):
        ingest_event(conn, RAW_BODY, headers(signature="deadbeef"), SECRET)
    row = conn.execute("SELECT action FROM audit_log").fetchone()
    assert row["action"] == "signature_rejected"


# --------------------------------------------------------------- deduplication

def test_first_event_is_stored(conn):
    assert ingest_event(conn, RAW_BODY, headers(), SECRET).result is IngestResult.STORED
    assert count(conn, "events") == 1


def test_same_event_three_times_yields_one_event(conn):
    results = [ingest_event(conn, RAW_BODY, headers(), SECRET).result for _ in range(3)]
    assert results == [IngestResult.STORED, IngestResult.DUPLICATE, IngestResult.DUPLICATE]
    assert count(conn, "events") == 1


def test_duplicates_are_audited_not_discarded(conn):
    for _ in range(3):
        ingest_event(conn, RAW_BODY, headers(), SECRET)
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["event_stored", "duplicate_suppressed", "duplicate_suppressed"]


def test_different_event_ids_stored_separately(conn):
    ingest_event(conn, RAW_BODY, headers(event_id="evt_A"), SECRET)
    ingest_event(conn, RAW_BODY, headers(event_id="evt_B"), SECRET)
    assert count(conn, "events") == 2


def test_missing_event_id_header_is_rejected(conn):
    h = headers()
    del h[EVENT_ID_HEADER]
    with pytest.raises(MissingEventIdError):
        ingest_event(conn, RAW_BODY, h, SECRET)
    assert count(conn, "events") == 0


# ------------------------------------------------------------------ storage

def test_raw_payload_preserved_byte_exact(conn):
    ingest_event(conn, RAW_BODY, headers(), SECRET)
    stored = conn.execute("SELECT raw_body FROM events").fetchone()["raw_body"]
    assert bytes(stored) == RAW_BODY


def test_headers_preserved(conn):
    ingest_event(conn, RAW_BODY, headers(), SECRET)
    stored = json.loads(conn.execute("SELECT headers_json FROM events").fetchone()["headers_json"])
    assert stored[EVENT_ID_HEADER] == "evt_TESTFIXTURE001"
    assert stored[SIGNATURE_HEADER] == compute_signature(RAW_BODY, SECRET)
    assert stored["user-agent"] == "Razorpay-Webhook"


def test_header_lookup_is_case_insensitive(conn):
    h = {k.upper(): v for k, v in headers().items()}
    assert ingest_event(conn, RAW_BODY, h, SECRET).result is IngestResult.STORED


def test_malformed_json_still_stored_with_null_event_type(conn):
    """A body we cannot parse is still evidence, provided the HMAC matches."""
    raw = b"{not valid json"
    out = ingest_event(conn, raw, headers(raw=raw), SECRET)
    assert out.result is IngestResult.STORED
    assert out.event_type is None
    assert bytes(conn.execute("SELECT raw_body FROM events").fetchone()["raw_body"]) == raw
