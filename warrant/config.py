"""
Frozen configuration for Warrant.

Everything here is an experiment-design assumption or a product policy.
Nothing here is a production measurement. See EXPERIMENT.md for provenance.
"""
from __future__ import annotations

# ---------------------------------------------------------------- determinism
SEED = 20260824
"""Master seed. Every stochastic component derives its stream from this."""

ASSIGNMENT_SALT = "warrant-v1"
"""Salt for hash(customer_id, salt) -> arm. Changing this reshuffles all arms."""


# ------------------------------------------------------------ experiment plan
# PRE-REGISTERED. Do not edit after the first results commit.
ALPHA = 0.05
POWER = 0.80
TWO_SIDED = True

P_HOLDOUT_ASSUMED = 0.34
"""Assumed 7-day ITT recovery rate in the untreated arm. DESIGN ASSUMPTION."""

P_WARRANT_ASSUMED = 0.39
"""Assumed 7-day ITT recovery rate under Warrant. DESIGN ASSUMPTION (+5pp)."""

OBSERVATION_WINDOW_DAYS = 7

# Minimum per-arm N for 80% power at alpha=0.05 two-sided to detect +5pp
# (0.34 -> 0.39). Derived via normal-approximation two-proportion formula.
PREREGISTERED_SAMPLE_SIZE = 568

PRIMARY_COMPARISON = ("WARRANT", "HOLDOUT")
SECONDARY_COMPARISON = ("RULES", "HOLDOUT")  # exploratory, not powered

ARMS = ("HOLDOUT", "RULES", "WARRANT")
ARM_WEIGHTS = {"HOLDOUT": 0.20, "RULES": 0.40, "WARRANT": 0.40}


# ------------------------------------------------------------- product policy
# These are OUR policies, not regulation. Labelled as such in the UI.
ACTION_COST_INR = {
    "SEND_PAYMENT_LINK": 55.0,
    "REMIND": 12.0,
    "RETRY": 8.0,
    "DO_NOTHING": 0.0,
}

EV_MARGIN_INR = 0.0
"""Expected value must exceed action cost by at least this margin to act."""

HIGH_VALUE_THRESHOLD_PAISE = 500_000
"""Carve-out threshold: a case with a ticket strictly above this is never held out.
Rs 5,000. Not every business can accept a holdout on its largest accounts, and
pretending otherwise would be dishonest product design (EXPERIMENT.md). Carved
cases are still measured -- they are excluded from HOLDOUT, not from the ledger."""

MAX_ATTEMPTS_PER_CASE = 3
FREQUENCY_CAP_PER_CUSTOMER_7D = 2
COHORT_SPEND_CEILING_INR = 5000.0

LLM_CONFIDENCE_FLOOR = 0.60
"""Below this, the proposer abstains and the case routes to DO_NOTHING."""


# ------------------------------------------------------------ regulation only
NPCI_AUTOPAY_MAX_ATTEMPTS = 4
"""VERIFIED: NPCI, effective Aug 2025 - one execution plus three retries per
UPI Autopay mandate cycle. Applies ONLY to case_type == 'upi_autopay'.
Does NOT apply to one-time payment links. Do not present as universal."""
