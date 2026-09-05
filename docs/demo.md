# Demo Walkthrough — Warrant

> A step-by-step walkthrough of the Warrant experiment system.
> Estimated time: 15 minutes. No Razorpay account needed — all data is synthetic.

---

## Before You Start

```bash
# Install dependencies
pip install -e .

# Set the webhook secret (required by the app)
export RAZORPAY_WEBHOOK_SECRET="test_secret_not_real"

# Start the server
python -m warrant.app
# Server is at http://localhost:8000
```

---

## 1. The Experiment Design

Open [EXPERIMENT.md](./EXPERIMENT.md) for the full analysis plan. Key facts:

| Parameter | Value |
|---|---|
| Arms | HOLDOUT (20%) / RULES (40%) / WARRANT (40%) |
| Primary metric | ITT recovery rate at 7 days |
| Min detectable effect | +5 pp (34% → 39%) |
| Preregistered N | 568 per arm = 1,704 total |
| Confidence interval | Newcombe Method 10, 90% CI |

**Three arms, not two.** RULES is the baseline (what we'd ship without Warrant). WARRANT is the treatment. HOLDOUT is the control.

---

## 2. The Dashboard

```bash
# In another terminal
open http://localhost:8000/dashboard
```

### Empty state

With no data, the dashboard shows:
- **SIM Lane: Idle** — no simulator cases yet
- **REAL Lane: Idle** — no webhook events yet
- **No arm cards** — preregistered sample size is 568 per arm
- **Config section** — shows all experiment parameters

### Lane badges explained

- **SIM Lane**: synthetic cases from the deterministic simulator. Active when `sim.py` has run.
- **REAL Lane**: real webhook events from Razorpay test mode. Active when `POST /webhooks/razorpay` has received events.

Both lanes write to the same ledger — there is no separate database for simulation vs production.

---

## 3. Running the Simulator

The simulator is deterministic: the same seed always produces the same cases. This lets you reproduce any result.

```python
from warrant.sim import Simulator
from warrant.core import assign
import warrant.db as db

sim = Simulator(seed=0)
conn = db.connect("warrant.db")
db.init_db(conn)

# Generate 100 cases
for case in sim.generate_batch(100):
    assignment = assign(
        conn,
        case.customer_id,
        case.case_id,
        case.ticket_amount_paise,
    )
    print(f"{case.case_id} → {assignment.arm}")

conn.close()
```

### What the simulator does

1. **Generate cases** with realistic error reasons (CARD_DECLINED, NETWORK_ERROR, etc.)
2. **Assign arms** using the same deterministic formula as production
3. **Draw ground truth** from hidden distributions (not visible to policy engine)
4. **Record outcomes** for the Newcombe interval demo

### Ground truth model

The simulator's ground truth is hidden from the policy engine:

| Segment | % of customers | Effect of treatment |
|---|---|---|
| RESPONSIVE | 10% | +12 pp recovery |
| NEUTRAL | 85% | 0 pp (self-heal only) |
| SLEEPING_DOG | 5% | -4 pp (treatment hurts) |

~34% of untreated cases self-heal within 7 days. The simulator does not use this to route cases — it's the hidden reality the experiment measures.

---

## 4. The Deterministic Assignment Formula

```python
from warrant.decide import assign

arm = assign(conn, customer_id, case_id, ticket_amount_paise)
# Returns: "HOLDOUT" | "RULES" | "WARRANT"
```

Assignment is `SHA256(customer_id + ASSIGNMENT_SALT) mod 100`:
- [0, 20) → HOLDOUT
- [20, 60) → RULES
- [60, 100] → WARRANT

The formula is deterministic: the same customer always gets the same arm, with no server-side state needed.

---

## 5. The Policy Engine

The policy engine decides what to do with a case assigned to RULES or WARRANT.

```python
from warrant.policy import evaluate

verdict = evaluate(conn, case)
# verdict.decision is EXECUTE or REFUSE
# verdict.reason explains the decision
```

### Decision gates (in order)

1. **Assignment check**: only RULES or WARRANT cases enter the policy
2. **Carve-out check**: cases above ₹5,000 ticket are carved out (measured, not held out)
3. **Attempt cap**: more than 3 attempts for this case → REFUSE
4. **Frequency cap**: this customer already got 2+ links in 7 days → REFUSE
5. **LLM confidence floor**: proposer confidence < 0.60 → REFUSE (abstain)
6. **EV gate**: expected value ≤ 0 → REFUSE
7. **EXECUTE**: the case passes all gates

---

## 6. The Intent Ledger

Every external action is recorded before the call is made.

```python
from warrant.act import execute, IntentStatus

intent = execute(conn, case, proposal, verdict, razorpay_caller)
print(intent.status)  # PENDING → EXECUTED or UNKNOWN
```

### Status lifecycle

```
PENDING → EXECUTED   (provider confirmed success)
PENDING → FAILED     (provider rejected the request)
PENDING → UNKNOWN    (timeout — don't know)
PENDING → CANCELLED  (order.paid arrived mid-intervention)
UNKNOWN → EXECUTED   (reconciliation confirmed success)
UNKNOWN → FAILED     (reconciliation confirmed failure)
```

### Idempotency

```python
# Three duplicate webhook deliveries → one intent, one provider call
result = sim.simulate_duplicate_webhook(conn, case, proposal, verdict)
# result[0].status == "EXECUTED"
# result[1].status == "EXECUTED"  (reused, no provider call)
# result[2].status == "EXECUTED"  (reused, no provider call)
```

The UNIQUE constraint on `idempotency_key` makes duplicate deliveries safe: the second and third deliveries find the existing intent and return it without calling the provider.

---

## 7. Reconciliation

When a timeout creates an UNKNOWN intent, reconciliation finds it and queries the provider.

```python
from warrant.ledger import get_pending_intents, resolve

# Find all UNKNOWN/PENDING intents older than 30 minutes
pending = get_pending_intents(conn, before_minutes=30)
for intent in pending:
    # Query Razorpay's list endpoint for this case's order
    confirmed_ref = razorpay_get_payment_link(intent.order_id)
    if confirmed_ref:
        resolve(conn, intent.intent_id, confirmed_ref)
```

`resolve()` records the provider's reference, which marks the intent as reconciled.

---

## 8. Confidence Intervals

The dashboard shows Newcombe Method 10 confidence intervals for the difference between arms.

```python
from warrant.stats import newcombe_diff_ci

diff = newcombe_diff_ci(s_t=200, n_t=568, s_c=160, n_c=568, alpha=0.10)
print(f"{diff.difference:+.1%}")   # +7.0 pp
print(f"[{diff.lower:+.1%}, {diff.upper:+.1%}]")  # [-1.2%, +15.2%]
```

**Interpretation:**
- If the CI **excludes zero**: the effect is statistically significant at the chosen α
- If the CI **includes zero**: we cannot rule out that there is no difference
- The **point estimate** is the observed difference

---

## 9. What to Show During the Demo

### 1. The empty dashboard (30 seconds)

Start with `http://localhost:8000/dashboard` showing the empty state. Point out:
- The SIM/REAL lane badges
- The preregistered N (568 per arm)
- The config section with all experiment parameters

### 2. Run the simulator (2 minutes)

```python
# In a Python REPL
from warrant.sim import Simulator
import warrant.db as db

sim = Simulator(seed=0)
conn = db.connect("warrant.db")
db.init_db(conn)

cases = sim.generate_batch(100)
print(f"Generated {len(cases)} cases")
```

### 3. Show the dashboard with data (1 minute)

Refresh the dashboard. With simulator cases, you see:
- SIM Lane: Active
- Arm cards with ITT metrics
- Newcombe CI bars for WARRANT vs HOLDOUT

### 4. The intent ledger (3 minutes)

Walk through a single case:
```python
# Show what happens to one case
case = cases[0]
assignment = assign(conn, case.customer_id, case.case_id, case.ticket_amount_paise)
print(f"Assigned to: {assignment.arm}")
```

### 5. The confidence interval (2 minutes)

Explain the CI bar:
- Zero line is at 50% of the bar
- Green fill means CI excludes zero (positive effect)
- Gray fill means CI includes zero (inconclusive)
- The point estimate is the observed difference

---

## 10. Key Demo Points

1. **Determinism**: the same seed produces the same result every time. No flaky tests.
2. **Idempotency**: duplicate webhooks are handled safely by the intent ledger.
3. **Timeout = UNKNOWN**: we don't claim to know what happened on a timeout. We check.
4. **Three arms**: we answer both "Warrant vs nothing" and "Warrant vs rules."
5. **Newcombe CI**: we show uncertainty, not just a point estimate.
6. **Hidden ground truth**: the simulator's ground truth cannot bias the policy engine.
