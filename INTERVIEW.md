# INTERVIEW.md — Panel Q&A Reference

> **This document is the authoritative reference for the panel Q&A. It was written
> before the experiment closed. Do not edit after the first results commit.**

---

## Experiment Design

### What are you measuring?

**Primary metric: Intent-to-treat (ITT) recovery rate at 7 days.**
Every assigned case counts — recovered, unresolved, carved-out, or lost to follow-up.
The denominator is every case assigned to an arm, not just the cases we acted on.
This is the conservative, unbiased estimator.

**Why ITT and not Treated-only?**
ITT is the right estimand for an experiment about a product decision: what is the
recovery rate for customers who land in this arm? Treated-only introduces selection
bias: we'd only count cases where the model was confident enough to act, which
overestimates effect size. The preregistered analysis plan specifies ITT.

**Secondary metrics:**
- Action count per arm (are we sending too many links?)
- Average ticket amount per arm
- Carve-out rate (cases above ₹5,000 threshold)

### Why three arms?

- **HOLDOUT (20%)**: Business as usual — the counterfactual. Provides the control
  rate for the experiment. Cases in this arm receive no automated intervention.
- **RULES (40%)**: The baseline model — rule-based heuristics tuned from historical
  data. This is what we would have shipped if Warrant wasn't ready.
- **WARRANT (40%)**: The treatment arm — Warrant's LLM-proposer plus deterministic
  policy gate.

Two treatment arms lets us answer: is Warrant better than the baseline rules, and
is the baseline rules better than doing nothing?

### Why not two arms?

A two-arm experiment (WARRANT vs HOLDOUT) only answers whether Warrant beats
nothing. It doesn't answer whether Warrant beats the alternative we'd ship instead.
With three arms we can distinguish "Warrant is better than rules" from "rules are
better than nothing."

### Why these arm weights?

20/40/40 (HOLDOUT/RULES/WARRANT) minimises variance on the primary comparison
(WARRANT vs HOLDOUT) while giving RULES enough sample to detect a moderate
rules-vs-holdout effect. The HOLDUT is the scarce resource — we give it the
minimum needed for 80% power, and put the rest into the treatment arms.

### What is the minimum detectable effect?

At 80% power, α=0.05 two-sided, with 568 per arm: **+5 percentage points**
(34% → 39%). This is the preregistered MDE. Effects smaller than this are
exploratory findings, not confirmatory results.

### What is the sample size?

**568 per arm = 1,704 total** (rounded up from the exact calculation to account
for carve-outs and lost-to-follow-up). The experiment stops when HOLDOUT reaches
568 assigned cases, not when we *feel ready*. Pre-registration is the discipline
that makes this credible.

### What happens if we hit the sample size on a weekend?

The simulator runs continuously and assignments happen as events arrive. If the
webhook fires on a Saturday, the assignment still happens. The clock for
observation window (7 days) starts at assignment. The analysis runs when we
check the dashboard — there is no automated stop because the simulator is
offline. In production, a cron job would check the counter and stop ingestion.

---

## Architecture

### Why is the intent ledger append-only?

The ledger records every external action we have taken (or tried to take) before
the call is made. If the process crashes mid-call, the intent survives and
reconciliation can find it. An actuator that calls first and records afterwards
has a window in which money has moved and nothing in our system knows it.

The append-only discipline also means the ledger is a complete audit trail:
every state the case has been in, every action we attempted, every reason for
a transition. No state is ever overwritten — a wrong transition is corrected by
appending a new one.

### What does the idempotency key guarantee?

The idempotency key is deterministic: `SHA256(case_id || '-' || attempt)[0..16]`.
The same (case, attempt) always produces the same key, so a redelivered webhook
with `attempt=1` returns the existing intent without making a second provider call.

The UNIQUE constraint on `idempotency_key` in the intents table is the enforcement
mechanism. Two concurrent deliveries both try to INSERT with the same key; the
second one gets `ON CONFLICT DO NOTHING` and returns the existing row.

**Important:** The key is ours, not Razorpay's. We don't rely on Razorpay's
idempotency support (which is documented for Payouts only). The guarantee is
our ledger.

### Why does a timeout mean UNKNOWN and not FAILED?

A timeout means we don't know whether the action happened. The request may have
been received, processed, and the payment link created — with only the response
lost. Marking that FAILED and retrying is how a customer gets two payment links.

UNKNOWN is the honest state: "we don't know, check the provider." The
reconciliation loop polls the provider's list endpoint to find out. Only when the
provider confirms a failure does the status become FAILED.

### Why is the ground truth hidden from the policy engine?

The simulator's ground truth (which customers would self-heal, which respond to
treatment) must not leak into the policy engine. If the policy knew that a
customer was in the RESPONSIVE segment, it could route them preferentially and
bias the experiment.

The isolation is enforced by:
1. `_Truth` is a module-level singleton that is not exported from `sim.py`
2. The `__all__` list in `sim.py` does not include `_Truth`
3. `warrant.policy` has no import of anything from `warrant.sim`
4. The test suite has a test that asserts this import boundary

### Why Newcombe Method 10 for confidence intervals?

Newcombe's Method 10 is a recommended approach for confidence intervals around a
difference of proportions. It is not based on the normal approximation, so it
doesn't break down near 0% or 100%. It is more accurate than the Wald interval
for the sample sizes we expect, and the implementation is straightforward.

The CI we show is 90%, not 95%, because this is an exploratory experiment and a
10% false-positive rate is acceptable for internal decision-making. We would use
95% for a confirmatory pre-registered result.

### Why is the arm assignment deterministic?

Assignment is `SHA256(customer_id + ASSIGNMENT_SALT) mod 100`, which maps to
[0,20) → HOLDOUT, [20,60) → RULES, [60,100] → WARRAT. The salt is a
configurable constant that can be changed between experiments (reshuffling all
arms) but never changed mid-experiment.

Determinism means:
- The same customer always gets the same arm (no customer assigned to two arms)
- The assignment is reproducible offline (simulator uses the same formula)
- There is no server-side state needed to know a customer's arm

---

## Production Considerations

### What would you need to add for production?

1. **Authentication on the dashboard**: currently `GET /dashboard` has no auth.
   Add session-based auth or IP allowlisting.
2. **Webhook secret validation**: the `RAZORPAY_WEBHOOK_SECRET` env var is read
   at startup. In production, this should be rotated and the process should
   reload without downtime.
3. **Database migrations**: currently schema is created at startup via `init_db()`.
   Production needs Alembic or similar for schema versioning.
4. **Rate limiting on the webhook endpoint**: add per-IP rate limits to prevent
   abuse.
5. **Reconciliation worker**: the `get_pending_intents()` function exists but
   the polling loop that calls the provider is not yet implemented as a worker.
6. **Alerting**: no alerts are wired up for the reconciliation loop. Production
   needs alerts on unresolved UNKNOWN intents older than 24 hours.
7. **Graceful shutdown**: the FastAPI lifespan context manager doesn't drain
   pending webhook requests before exit.

### How does the experiment stop?

There is no automatic stop in the simulator. The dashboard shows the current
HOLDOUT count. The operator stops the experiment by:
1. Noting the HOLDOUT count at 568
2. Waiting 7 more days for the observation window
3. Reading the dashboard for the final ITT numbers

In a production deployment, a cron job would check `COUNT(*)` for HOLDOUT and
stop calling `assign()` when it reaches the preregistered N.

### How would you extend this to other failure types?

The current failure taxonomy is specific to Razorpay payment failures. To extend:
1. Add new case types in the simulator (e.g., `refund_pending`)
2. Add new action types in the policy engine (e.g., `INITIATE_REFUND`)
3. Add new state transitions in the state machine
4. The intent ledger and arm assignment are generic — no changes needed

---

## Known Limitations

- **Single-currency**: amounts are stored in paise. Multi-currency support would
  need a currency field and conversion logic.
- **No customer suppression**: a customer could appear in multiple cases and receive
  multiple payment links. `FREQUENCY_CAP_PER_CUSTOMER_7D` is in the config but
  not yet enforced in `assign()`.
- **No A/B test for the proposer model**: the LLM model (e.g., GPT-4o) is fixed.
  Testing two proposer models would need a third arm and a second experiment.
- **Simulation offline**: the simulator produces synthetic cases for the demo.
  Production would use real webhook events from Razorpay's test mode.
- **No observability stack**: no structured logging, no metrics, no traces. The
  dashboard is the only visibility surface.
