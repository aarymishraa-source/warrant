# Warrant

**Decide whether recovery is warranted.**

Recovery rate tells us what happened after an intervention. It doesn't tell us what the intervention caused. A recovery system that sends payment links to everyone can claim credit for customers who would have paid anyway.

Warrant's purpose is to measure which recoveries were actually caused by intervention — and spend money only when the expected incremental recovery justifies the cost.

`DO_NOTHING` is a scored economic decision, not a fallback.

## The problem in one paragraph

A payment fails. The recovery system sends a link. The customer pays. Recovery rate: +1.

But the customer was going to pay anyway — within 24 hours, without any message. The recovery system has not caused recovery. It has caused noise. Every recovery system that measures only "what happened" overcounts cases that self-resolve. Warrant uses a deterministic holdout to estimate the counterfactual, then acts only when the expected incremental value exceeds the cost of action.

## How it works

```
payment.failed webhook
  → deterministic assignment (SHA256)
  → HOLDOUT / RULES / WARRANT arm
  → model proposes action + confidence
  → deterministic policy evaluates gates
  → intent persisted before provider call
  → provider called
  → reconciled
  → measured
```

**Assignment** is deterministic: `SHA256("warrant-v1:" + customer_id) % 100`. Bucket 0–19 → HOLDOUT (never treated). Bucket 20–59 → RULES. Bucket 60–99 → WARRANT. The same customer always gets the same arm.

**Model proposes.** The LLM proposer returns `{action, confidence, rationale}` validated by Pydantic. Invalid output or confidence below 0.60 abstains. The model can only cause refusal — it cannot approve, widen a cap, or call a payment API.

**Policy decides.** Nine gates in order: holdout, terminal state, attempt cap (3), NPCI autopay cap (4, UPI only), frequency cap (2 per customer per 7 days), cohort spend ceiling (₹5,000), confidence floor, unknown action, expected value. Any gate fires → refusal. No gate fires → execution.

**Intent before call.** The intent row is committed before the external provider is called. If the process dies between the two, reconciliation finds the intent and queries the provider for truth. A call-then-record pattern has a window in which money moved and the system doesn't know it.

**Unknown is not failed.** A provider timeout means we do not know what happened. Status becomes `UNKNOWN`, not `FAILED`. The only path to `FAILED` is a `ProviderRejected` exception — a positive claim from the provider that nothing was created. Retry requires reconciliation first: list the provider's objects, then decide.

## Experiment design

Three arms, ITT denominators, Newcombe Method 10 on the difference of two proportions.

| Arm | Allocation | Role |
|---|---|---|
| HOLDOUT | 20% | Counterfactual baseline. Never treated. |
| RULES | 40% | Deterministic rule table. |
| WARRANT | 40% | LLM proposer + policy engine. |

Primary comparison: WARRANT vs HOLDOUT. Secondary (exploratory): RULES vs HOLDOUT.

Denominator: all assigned cases, including `DO_NOTHING`, refused, and failed. Removing cases that didn't receive intervention inflates the estimate by exactly the mechanism this project exists to expose.

CI methodology: Newcombe Method 10 (Wilson intervals composed on the difference). Two overlapping arm CIs do not constitute a test of the difference. The estimand is `p_treat - p_control` and the interval must be built on that quantity directly.

Sample size: 1,452 per arm at 80% power, alpha=0.05, to detect +5pp (34% → 39%).

## What the simulation found

The current simulation uses a deterministic seed (20260824) and a hardcoded +5pp design uplift. It is not production evidence. The simulation result:

```
WARRANT:  34.2%  (235/688)
HOLDOUT:  47.8%  (160/335)
DIFFERENCE: -13.6pp
90% CI:   [-19.0, -8.2]
```

The simulation did not demonstrate a positive Warrant treatment effect. This is meaningful: it shows what a failed experiment looks like. A real experiment that produces this result should be reported honestly, not buried. The holdout prevents false attribution — the system correctly observed that the intervention did not cause incremental recovery.

## What's implemented, what's not

Implemented:
- Deterministic assignment (SHA256, salt `warrant-v1`)
- HOLDOUT gate as the first gate in the policy chain
- Policy engine with nine gates and EV arithmetic
- Intent ledger with `PENDING → EXECUTED / FAILED / UNKNOWN / CANCELLED`
- `UNKNOWN ≠ FAILED` discipline enforced in code
- Intent committed before provider call
- HMAC-SHA256 webhook verification over raw bytes
- Event deduplication at UNIQUE constraint level
- `cancel_pending()` on `order.paid`
- Newcombe Method 10 confidence interval
- Deterministic seeded simulator with hidden ground truth
- Landing page and experiment dashboard

Not implemented (do not claim as working):
- Live LLM API integration (Pydantic interface stub only)
- Production Razorpay API wiring (test-mode POC scripts only)
- Automated reconciliation worker
- Cloud deployment
- Production webhook endpoint

## Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest tests\ -q        # 236 tests
python -m uvicorn warrant.app:app --port 8000 --reload
```

`GET /` → landing page. `GET /dashboard` → experiment dashboard (auto-seeds 6 simulator cases on empty DB). `GET /health` → `{"status": "ok"}`.

`RAZORPAY_WEBHOOK_SECRET` must be set as an environment variable. See `.env.example`.

## Architecture

```
warrant/
├── act.py        — intent ledger and actuator (intent-before-call, UNKNOWN≠FAILED)
├── assign.py     — deterministic arm assignment (SHA256, pure functions)
├── config.py     — frozen constants (experiment plan, policy thresholds, costs)
├── core.py       — case state machine with optimistic version guard
├── db.py         — SQLite schema
├── decide.py     — LLM proposer interface + rules baseline
├── ingest.py     — webhook verification and deduplication (HMAC, raw bytes)
├── ledger.py     — reconciliation queries
├── policy.py     — deterministic gate-based policy engine
├── sim.py        — seeded synthetic simulator (ground truth hidden from policy)
├── stats.py      — Newcombe CI, sample size (no scipy)
└── app.py        — FastAPI surface
```

## Docs

- `EXPERIMENT.md` — pre-registered analysis plan
- `CALIBRATION.md` — simulator parameter provenance
- `FAILURES.md` — what broke and how it was fixed
- `NOTES.md` — working reference, day-by-day plan
- `INTERVIEW.md` — panel Q&A reference
- `docs/demo.md` — step-by-step walkthrough
- `scripts/power.py` — reproduces every number in EXPERIMENT.md
