"""
Independently fetch a payment from the Razorpay API and compare it against
what the webhook told us. Usage:  python check_payment.py pay_XXXXXXXXXXXX

Why this exists: webhooks can be late, duplicated, or out of order. The API is
the source of truth. LIFT's state machine will rely on this reconciliation, so
we prove it works on Day 1.
"""
import json
import os
import sys

import requests

KEY_ID = os.environ["RAZORPAY_KEY_ID"]
KEY_SECRET = os.environ["RAZORPAY_KEY_SECRET"]
assert KEY_ID.startswith("rzp_test_"), "Refusing to run: not a TEST key."

if len(sys.argv) != 2:
    sys.exit("usage: python check_payment.py pay_XXXXXXXXXXXX")

payment_id = sys.argv[1]

resp = requests.get(
    f"https://api.razorpay.com/v1/payments/{payment_id}",
    auth=(KEY_ID, KEY_SECRET),
    timeout=30,
)
print("HTTP", resp.status_code)
body = resp.json()
print(json.dumps(body, indent=2))

print("\n--- API view ---")
for f in ("id", "order_id", "status", "method",
          "error_code", "error_description",
          "error_reason", "error_source", "error_step"):
    print(f"{f:20s} : {body.get(f, '<ABSENT>')!r}")
