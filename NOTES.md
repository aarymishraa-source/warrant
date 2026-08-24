# Warrant — working reference

Keep this. It is everything you need to continue without the original chat.

## Frozen spec

**Track:** 03 — AI Revenue Recovery
**Name:** Warrant. Do not rename.
**Thesis:** Recovery rate tells us what happened after an intervention.
It doesn't tell us what the intervention caused.

**Product:** A counterfactual recovery controller. On payment failure it asks
"should we intervene at all?" — estimates the chance the customer recovers
unaided, computes whether intervening clears its own cost, applies
deterministic policy gates, then ACTS or deliberately does NOTHING.

`DO_NOTHING` is a scored economic decision, not a fallback.

## Two demo moments (everything serves these)

1. **The number gets smaller.** Gross recovered → incremental caused.
2. **The agent refuses to act.** LLM proposes SEND_PAYMENT_LINK; policy engine
   rejects on `EV ₹34 < cost ₹55`; final action DO_NOTHING; hidden ground truth
   then shows the customer paid on day 3 anyway.

## Days remaining

| Day | Module | Deliverable |
|-----|--------|-------------|
| 4  | `core/`   | case state machine, append-only transitions, version guard, authoritative re-fetch |
| 5  | `assign/` | deterministic hash(customer_id, salt) → arm, carve-outs before assignment, balance test |
| 6  | `policy/` | hard gates, one unit test per gate proving rejection |
| 7  | `act/`    | intent-before-call, timeout → UNKNOWN, reconcile by listing |
| 8  | `decide/` | rules lookup + LLM proposer (Pydantic), abstention, rules-vs-LLM eval table |
| 9  | `sim/`    | seeded, hidden ground truth, 3 injections, import-boundary test |
| 10 | `ledger/` + UI | ITT, Newcombe CI, one page, SIM/REAL badges |
| 11 | buffer    | nothing new starts; cut what doesn't work |
| 12 | ship      | video, README, ARCHITECTURE.md, FAILURES.md |

## AI boundary

LLM does exactly three things: free-text `error_description` → root cause **only
where structured fields are empty**; propose `{action, timing, channel,
rationale}`; abstain when uncertain.

LLM never: calls a payment API, computes final EV, sets a limit, decides
compliance, overrides a gate, or writes customer-facing free text (it picks a
`template_id` and fills typed slots).

Malformed LLM output → reject → DO_NOTHING → audit → test.

## Payment safety rules

- HMAC over **raw bytes**. Never `json.dumps(json.loads(x))`.
- Dedup on `x-razorpay-event-id` via a UNIQUE constraint, not SELECT-then-INSERT.
- Duplicate delivery is normal. Return 200, audit it.
- Webhooks are unordered. Version guard prevents state regression.
- **Timeout = UNKNOWN, never FAILED.** Persist intent before the call, reuse the
  same internal idempotency key, reconcile by listing.
- `order.paid` cancels pending recovery intents.
- Razorpay documents idempotency for Payouts only. For Orders/Payments/Payment
  Links it is undocumented — we keep our own intent ledger. Do not claim otherwise.

## Experiment

Primary: WARRANT − HOLDOUT, 7-day recovery rate, intention-to-treat.
Denominator includes DO_NOTHING cases. Design: 34% → 39% (+5pp), n=1452/arm,
alpha 0.05 two-sided, power 0.80. Interval: Newcombe on the **difference**.
RULES arm is secondary/exploratory. See `EXPERIMENT.md`.

## Never do

- Present synthetic results as production lift
- Let the LLM move money or set a limit
- Change `stats.py` without a demonstrated mathematical error
- Add a feature not in the day table above (default answer to scope: NO)
- Claim the RBI 08:00–19:00 window applies to merchant dunning — it is a
  lending/recovery-agent rule. UNVERIFIED for this use case, therefore unused.
- Reuse the discarded "61% vs 34%" framing. 27pp needs 50/arm, not 1500.

## Open gate (do this first)

`poc/payloads/` is empty. Run the Day-1 gate and check whether `error_reason`,
`error_source`, `error_step` are populated on a real test-mode failure.

- **Populated** → deterministic lookup is the primary diagnosis path (spec as written)
- **Empty** → free-text becomes primary and Day 8 grows

Instructions: `poc/README.md`

## Prompt for any coding agent

> Here is the Warrant repo. Read NOTES.md, EXPERIMENT.md, and
> tests/test_ingest.py first to learn the conventions. Today is Day N:
> [module]. Build only that. Write tests. Run them. Show me the exact
> commands and output. Do not expand scope. Do not touch stats.py.
