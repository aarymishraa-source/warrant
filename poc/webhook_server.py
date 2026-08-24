"""
LIFT Day-1 gate: Razorpay TEST-MODE webhook receiver.

Single endpoint: POST /webhooks/razorpay
Purpose: prove that a real test-mode payment failure produces a signed,
verifiable payment.failed webhook with usable error evidence.

Nothing else. No database, no policy, no AI.
"""
import datetime
import hashlib
import hmac
import json
import os
import pathlib

from flask import Flask, request

SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"].encode()
OUT = pathlib.Path(__file__).parent / "payloads"
OUT.mkdir(exist_ok=True)

ERROR_FIELDS = (
    "error_code",
    "error_description",
    "error_reason",
    "error_source",
    "error_step",
)

# Fields redacted in the *shareable* copy only. The raw copy stays local.
PII_KEYS = {"email", "contact", "customer_email", "customer_contact", "vpa", "name"}

app = Flask(__name__)


def redact(node):
    """Recursively mask PII values so the payload can be pasted publicly."""
    if isinstance(node, dict):
        return {
            k: ("<REDACTED>" if k in PII_KEYS and v else redact(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [redact(x) for x in node]
    return node


@app.post("/webhooks/razorpay")
def razorpay_webhook():
    # CRITICAL: raw bytes. Re-serialising (json.dumps(request.json)) changes
    # whitespace/ordering and the HMAC will never match.
    raw = request.get_data()

    sig = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(expected, sig)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    event_id = request.headers.get("X-Razorpay-Event-Id", "no-event-id")
    stem = OUT / f"{ts}__{event_id}"

    # Persist before parsing, so a crash below never loses evidence.
    stem.with_suffix(".raw.json").write_bytes(raw)
    stem.with_suffix(".headers.json").write_text(
        json.dumps(dict(request.headers), indent=2)
    )

    print("\n" + "=" * 64)
    print(f"signature_valid      : {valid}")
    print(f"x-razorpay-event-id  : {event_id}")
    print(f"saved                : {stem.with_suffix('.raw.json').name}")

    if not valid:
        print("!! SIGNATURE MISMATCH - not processing. Payload saved for inspection.")
        print("=" * 64)
        return "", 400

    body = json.loads(raw)
    stem.with_suffix(".redacted.json").write_text(
        json.dumps(redact(body), indent=2)
    )

    print(f"event                : {body.get('event')}")

    entity = (body.get("payload", {}).get("payment", {}) or {}).get("entity", {})
    print(f"payment_id           : {entity.get('id')}")
    print(f"order_id             : {entity.get('order_id')}")
    print(f"status               : {entity.get('status')}")
    print(f"method               : {entity.get('method')}")
    print("-" * 64)
    for field in ERROR_FIELDS:
        if field not in entity:
            shown = "<FIELD ABSENT FROM PAYLOAD>"
        else:
            shown = repr(entity[field])
        print(f"{field:20s} : {shown}")
    print("=" * 64)
    print(f"share this file      : {stem.with_suffix('.redacted.json').name}")

    return "", 200


if __name__ == "__main__":
    app.run(port=5000)
