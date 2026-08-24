# Warrant

**Decide whether recovery is warranted.**

Recovery rate tells us what happened after an intervention.
It doesn't tell us what the intervention caused.

Warrant is a counterfactual payment-recovery controller. When a payment fails
it does not ask "what message should we send?" It asks "should we intervene at
all?" - estimates the probability the customer recovers unaided, computes
whether intervening clears its own cost, applies deterministic policy gates,
and then either acts or deliberately does nothing.

DO_NOTHING is a scored economic decision, not a fallback.

## Status - Day 3 of 12

Done: pre-registered analysis plan, calibration doc, statistics module
(power calculation + Newcombe interval), webhook ingest with raw-body HMAC
and event_id deduplication. 28 tests passing.

Not done: Razorpay Day-1 gate, core, assign, decide, policy, act, ledger, sim.

Nothing is claimed to work that has not been executed.

## Run

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest tests\ -q

## Two lanes, never mixed

REAL - RAZORPAY TEST MODE. Live test-mode interactions. Proves payment
engineering correctness.

SYNTHETIC - CORRECTNESS TEST. Seeded generator with ground truth the decision
layer cannot import. Not evidence of production lift.

## Design decisions worth defending

Newcombe interval on the difference, not two per-arm intervals. Overlap
between arm CIs is not a test of the difference.

ITT denominators. Cases where the controller chose DO_NOTHING stay in the
denominator. Removing them would inflate the estimate by exactly the
mechanism this project exists to expose.

No provider idempotency assumed. Razorpay documents idempotency for Payouts.
For Orders, Payments and Payment Links it is undocumented, so Warrant keeps
its own intent ledger.

Regulation scoped honestly. The NPCI one-execution-plus-three-retries cap
applies only to UPI Autopay cases. Everything else is product policy.

## Docs

EXPERIMENT.md, CALIBRATION.md, FAILURES.md, NOTES.md
