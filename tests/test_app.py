"""HTTP-level tests for the webhook endpoint. No network, no Razorpay account."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from tests.test_ingest import RAW_BODY, SECRET, headers


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("WARRANT_DB", str(tmp_path / "test.db"))
    from warrant import app as app_module
    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c


def post(client, raw=RAW_BODY, hdrs=None):
    return client.post("/webhooks/razorpay", content=raw, headers=hdrs or headers())


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_valid_delivery_returns_200_stored(client):
    r = post(client)
    assert r.status_code == 200
    assert r.json()["result"] == "stored"


def test_invalid_signature_returns_400(client):
    r = post(client, hdrs=headers(signature="deadbeef"))
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_signature"


def test_duplicate_delivery_returns_200_not_error(client):
    """A duplicate is handled correctly, so a non-2xx would trigger a pointless
    redelivery from the sender."""
    assert post(client).json()["result"] == "stored"
    for _ in range(2):
        r = post(client)
        assert r.status_code == 200
        assert r.json()["result"] == "duplicate"


def test_body_is_read_as_raw_bytes(client):
    """Whitespace that a JSON round-trip would normalise must still verify."""
    spaced = b'{"event": "payment.failed",  "contains": ["payment"]}'
    r = post(client, raw=spaced, hdrs=headers(raw=spaced))
    assert r.status_code == 200
