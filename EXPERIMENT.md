# Pre-registered analysis plan

**Committed before any results-producing code exists.** Check `git log` — this
file's commit precedes the first commit touching `warrant/ledger.py` or `sim/`.
That ordering is the point of this document.

## Estimand

Difference in 7-day recovery rate between the WARRANT arm and the HOLDOUT arm,
measured **intention-to-treat**.

## Primary comparison

`WARRANT − HOLDOUT`. Pre-registered, powered, one comparison.

`RULES − HOLDOUT` is **secondary and exploratory**. It is reported without a
significance claim. Declaring one primary comparison in advance is the whole of
our multiplicity handling, and it is sufficient for a two-arm design.

## Denominator (the part people get wrong)

Every case assigned to an arm belongs to that arm's denominator — including
cases where the controller chose `DO_NOTHING`, where a policy gate rejected the
action, and where the action failed to execute.

Excluding no-action cases from the WARRANT denominator would inflate the
estimate by exactly the mechanism this project exists to expose.

## Assignment

`arm = f(sha256(customer_id + ASSIGNMENT_SALT))`, deterministic, at the
**customer** level. Randomising per-invoice would leak treatment across a
customer's own cases.

Assignment happens **before** any model or heuristic reads the case. Carve-outs
are applied before assignment and logged with a reason:

- high-value accounts above a configured ticket threshold
- `case_type == 'lending_emi'`

Carved-out cases are never held out. Not every business can accept a holdout on
every customer, and pretending otherwise would be dishonest product design.

## Design assumptions

| Parameter | Value | Status |
|---|---|---|
| Assumed holdout recovery rate | 34% | **DESIGN ASSUMPTION** |
| Assumed Warrant recovery rate | 39% | **DESIGN ASSUMPTION** |
| Effect size | +5pp | **DESIGN ASSUMPTION** |
| alpha | 0.05, two-sided | pre-registered |
| power | 0.80 | pre-registered |
| **n per arm** | **1,452** | derived, see below |
| Observation window | 7 days | pre-registered |

+5pp is deliberately modest. Real payment interventions move single-digit
points. Sizing for a large effect would signal we had never seen one.

## Sample size derivation

```
n = (z_{alpha/2} + z_beta)^2 * [p1(1-p1) + p2(1-p2)] / (p1 - p2)^2
  = (1.95996 + 0.84162)^2 * [0.34*0.66 + 0.39*0.61] / 0.05^2
  = 7.8489 * 0.4623 / 0.0025
  = 1451.5  ->  1452
```

Reproduce: `python scripts/power.py`. Asserted in `tests/test_stats.py`.

## Interval

**Newcombe Method 10** on the difference of proportions.

We do not, and will not, reason from whether the two per-arm intervals overlap.
That is not a test of the difference. The estimand is `p1 - p2`, so the interval
is built on `p1 - p2`.

## Stopping rules

**Statistical:** fixed n = 1,452 per arm. No interim analysis. No peeking. The
experiment is analysed once.

**Business** (independent of the above, enforced by the policy engine):
- max 3 intervention attempts per case
- max 2 customer contacts per rolling 7 days
- cohort spend ceiling
- hard stop on any gate violation

## What we will claim

> In a seeded synthetic environment, the controller produces a +5pp effect over
> holdout, and the measurement pipeline recovers that effect with the stated
> interval and coverage.

## What we will never claim

- any production recovery rate
- any rupee figure as a business result
- that the simulator demonstrates real-world lift
- time-to-recovery, churn prevented, or LTV effects

## Null results

If the exploratory RULES arm produces an interval crossing zero, that is
reported as a null. Verified example from `tests/test_stats.py`:
`+1.4pp, 95% CI [-2.1pp, +4.8pp]` — reported as **not significant**.

## Discarded framing

An earlier draft paired "61% vs 34%" with n=1500/arm. A 27pp effect requires
**50** per arm, not 1500. The pairing was internally inconsistent and is
permanently removed. `tests/test_stats.py::test_large_effect_needs_far_fewer`
exists to keep it dead.
