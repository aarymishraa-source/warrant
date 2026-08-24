"""
Creates ONE Razorpay test-mode Payment Link and prints its short_url.
Endpoint: POST https://api.razorpay.com/v1/payment_links  (Basic Auth, test keys)
"""
import json
import os

import requests

KEY_ID = os.environ["RAZORPAY_KEY_ID"]
KEY_SECRET = os.environ["RAZORPAY_KEY_SECRET"]

assert KEY_ID.startswith("rzp_test_"), "Refusing to run: this is not a TEST key."

payload = {
    "amount": 10000,  # paise -> Rs 100.00
    "currency": "INR",
    "description": "LIFT test-mode failure POC",
    "notify": {"sms": False, "email": False},  # do not message anyone
    "reminder_enable": False,
    "notes": {"poc": "payment_failed_webhook_validation"},
}

resp = requests.post(
    "https://api.razorpay.com/v1/payment_links",
    auth=(KEY_ID, KEY_SECRET),
    json=payload,
    timeout=30,
)

print("HTTP", resp.status_code)
body = resp.json()
print(json.dumps(body, indent=2))

if resp.ok:
    print("\nOPEN THIS IN A BROWSER:")
    print(body.get("short_url"))
    # NOTE: whether `order_id` is present in this response is UNKNOWN for the
    # plink_* API. We do not depend on it — the order_id we care about arrives
    # in the payment.failed webhook payload.
