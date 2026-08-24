# LIFT — Day 1 gate

Purpose: prove one thing only.

> Can we reliably receive and inspect a real `payment.failed` webhook from a
> Razorpay **test-mode** payment, with enough error evidence to classify it?

Nothing here is LIFT. This is the gate that decides whether LIFT is buildable.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill it in
set -a && source .env && set +a

# terminal 1
python webhook_server.py

# terminal 2
ngrok http 5000           # copy the https URL into the Dashboard webhook

# terminal 3 (after the webhook is configured in the Dashboard)
python create_link.py     # open the printed short_url in a browser
```

Then trigger the failure in the browser (see below), watch terminal 1, and
finally cross-check:

```bash
python check_payment.py pay_XXXXXXXXXXXX
```

## Failure trigger

Primary: pay by **card** → Razorpay's test-mode mock bank page → click
**Failure**.

Do **not** rely on typing `failure@razorpay` as a UPI ID. Razorpay's docs carry
an NPCI notice that UPI Collect (manually entered VPA) was deprecated effective
28 Feb 2026. Status: **LIKELY NOT SUPPORTED — verify if you try it.**

Card numbers: read them from Razorpay's own test-integration / Cards Error Codes
docs. Do not use card numbers from memory or from a blog. Any number not read
off the official docs is **UNKNOWN**.

## Pass criteria

1. `POST /v1/payment_links` returns 200 with a `short_url` on an `rzp_test_` key
2. A request reaches `/webhooks/razorpay`
3. `signature_valid : True`
4. `event : payment.failed`
5. `payment_id` non-null
6. `order_id` non-null
7. `error_code` **and** `error_description` non-null and non-empty
8. `check_payment.py` agrees with the webhook on `status` and error fields

## Known unknowns

- Whether `error_reason` / `error_source` / `error_step` populate in test mode:
  **UNKNOWN — this run is the verification.**
- Idempotency keys on Orders / Payments / Payment Links: **undocumented.** We
  send none and assume none. LIFT builds its own ledger.
- Whether the plink API response contains `order_id`: **UNKNOWN.** We don't
  depend on it; the `order_id` we use comes from the webhook.

## Files

```
create_link.py     POST /v1/payment_links
webhook_server.py  POST /webhooks/razorpay  (verify → persist → print)
check_payment.py   GET  /v1/payments/:id    (reconciliation check)
payloads/          raw + headers + redacted copies
```
