# Simulator calibration

Every parameter the synthetic generator uses, with its provenance.

**Everything in this file is SYNTHETIC.** The simulator is a correctness
harness. It does not model the Indian payments market and is not evidence
about it.

## Provenance labels

- **VERIFIED** — from a primary or reputable secondary source, cited
- **DESIGN** — chosen by us to make the correctness test meaningful
- **ARBITRARY** — chosen for convenience; nothing depends on the exact value

## Determinism

| Parameter | Value | Status |
|---|---|---|
| `SEED` | 20260824 | DESIGN |
| `ASSIGNMENT_SALT` | `warrant-v1` | DESIGN |

Every stochastic component derives its stream from `SEED`. `make reproduce`
from a clean clone must print identical numbers.

## Failure causes

Sampled from Razorpay's published payment error taxonomy
(`error_source` x `error_step` x `error_reason`) rather than invented.
Source: Razorpay error-codes documentation and the published
`payments_error_reasons` list.  **Status: VERIFIED (taxonomy), DESIGN (mix).**

The relative frequency of each cause is DESIGN. We are not claiming to know
the real distribution.

## Self-heal behaviour

| Parameter | Value | Status |
|---|---|---|
| `p_self_heal` by root cause | 0.10 - 0.55 | DESIGN |
| Self-heal window | 1-6 days | DESIGN |
| Overall untreated 7d recovery | ~0.34 | DESIGN (matches EXPERIMENT.md) |

Direction, not magnitude, is grounded: a material share of failed payments
resolve without any intervention, which is the premise of the product.

## Treatment response

| Parameter | Value | Status |
|---|---|---|
| Uplift, responsive segment | +0.12 | DESIGN, hardcoded |
| Uplift, sleeping-dog segment | -0.04 | DESIGN, hardcoded |
| Uplift, self-healers | ~0.00 | DESIGN, hardcoded |
| Population-weighted uplift | +0.05 | DESIGN |

**Uplift is hardcoded, not modelled.** No T-learner, no uplift model. The
demo shows a controller declining to act on a negative-uplift segment; how
the segment was estimated is not what is being tested.

## Case types

| Type | Share | Notes |
|---|---|---|
| `one_time_link` | 0.70 | product policy caps only |
| `upi_autopay` | 0.30 | NPCI 1+3 gate applies |

**NPCI constraint — VERIFIED:** effective August 2025, a UPI Autopay mandate
permits one execution plus three retries per cycle (four attempts total),
after which the cycle is marked failed. Autopay executions are additionally
restricted to non-peak windows.

**Scoped correctly:** this gate fires only on `case_type == 'upi_autopay'`.
It does not apply to one-time payment links.

**Explicitly NOT claimed:** the RBI 08:00-19:00 contact window is a *digital
lending / recovery agent* rule. We do not assert it governs subscription or
D2C dunning. Our frequency and timing caps are labelled product policy, not
regulation. UNVERIFIED for this use case; therefore unused.

## Costs

| Action | Cost | Status |
|---|---|---|
| `SEND_PAYMENT_LINK` | Rs 55 | ARBITRARY |
| `REMIND` | Rs 12 | ARBITRARY |
| `RETRY` | Rs 8 | ARBITRARY |

Arbitrary, and deliberately so. We never compute a weighted "net value"
scalar from invented coefficients. Costs enter only the EV gate, where they
are visible on screen and can be argued with.
