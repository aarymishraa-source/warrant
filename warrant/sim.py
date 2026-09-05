"""
Seeded payment-failure simulator.

Produces deterministic synthetic cases for offline development, integration testing,
and the Newcombe confidence-interval demo. The ground truth -- which cases would
have self-healed, and which customers respond to intervention -- is generated from
the same seed and is therefore reproducible. It is hidden: not exported, not
importable by policy.py, decide.py, or act.py. The simulation is an island.

Architecture
------------
- `config.SEED` (20260824) is the master seed. All randomness derives from it.
- `_rng()` returns a `random.Random` instance seeded from the master seed. This
  lets individual subsystems (case generation, ground-truth drawing) be reset to
  a known point without reseeding the module-level RNG globally.
- `_Truth` holds hidden ground-truth draws (customer segments, self-heal flags).
  It is constructed once when the module is first used and is not surfaced.
- `Simulator` runs the simulation loop: generate a case, evaluate ground truth,
  optionally invoke the decision policy (via injected callable), record outcome.

Case generation
---------------
Cases are generated deterministically from the master seed, cycling through the
two calibrated case types (`one_time_link`, `upi_autopay`) and using a realistic
distribution over error reasons. Ticket amounts follow a log-normal distribution
centred around ₹250.

Ground truth model
------------------
~34% of untreated cases self-heal within the observation window (7 days). This
matches `P_HOLDOUT_ASSUMED` from EXPERIMENT.md.

Customer segments (drawn once per unique customer_id, stored in _Truth):
  RESPONSIVE  10% of customers  → +12 pp recovery rate under treatment
  SLEEPING_DOG  5% of customers →  -4 pp recovery rate under treatment
  NEUTRAL    85% of customers  →   0 pp (self-heal only)

Recovery under treatment:
  - RESPONSIVE + self-heal: probability = P_self_heal + 0.12
  - SLEEPING_DOG + self-heal: probability = P_self_heal - 0.04 (capped at 0)
  - NEUTRAL + self-heal: probability = P_self_heal
  - No treatment: P_self_heal (34%)

The simulator does NOT call the policy engine or the proposer. It is the
authority for what "would have happened" in each arm, for the Newcombe interval
demo.

Failure injections
------------------
Three specific failure scenarios are available as explicit method calls. These
exercise the reconciliation, cancellation, and timeout paths in act.py without
requiring a live Razorpay account.

A. Duplicate webhook redelivery: `simulate_duplicate_webhook(case)` — simulates
   the same event arriving three times. The intent ledger's UNIQUE constraint on
   `idempotency_key` is what makes this safe: the second and third executions
   are suppressed before the external call.

B. order.paid arriving mid-intervention: `simulate_order_paid(case)` — calls
   `act.cancel_pending` to cancel every PENDING or UNKNOWN intent for the case's
   order. This exercises the cancellation path without needing a real payment.

C. Action timeout: `simulate_action_timeout(proposal, verdict)` — calls
   `act.execute` with a caller that raises `TimeoutError`, producing an intent
   with status UNKNOWN. This exercises the timeout path without a live network.

Import boundary
---------------
`_Truth`, `_rng`, and all ground-truth drawing are in this module and are NOT
importable from `warrant.policy`, `warrant.decide`, or `warrant.act`. The
`tests/test_sim.py::test_import_boundary` assertion enforces this.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from warrant import config
from warrant.act import cancel_pending, execute as act_execute, IntentStatus
from warrant.policy import Proposal, Verdict

__all__ = ["Simulator", "SimulatorCase", "CustomerSegment"]


# ------------------------------------------------------------------------- master seed

def _rng(sequence: int = 0) -> random.Random:
    """Return a Random instance seeded from config.SEED plus a sequence number.

    Call with different sequence numbers to get independent but reproducible
    streams. Call with the same sequence number to re-seed to a known point.
    """
    rng = random.Random()
    # Mix the sequence number into the master seed so Simulator(seed=0) and
    # Simulator(seed=1) produce genuinely different case streams.
    combined = (config.SEED << 16) ^ (sequence * 2654435771)
    rng.seed(combined & 0xFFFFFFFF)
    return rng


# ------------------------------------------------------------------------- ground truth (hidden, not exported)

class CustomerSegment(str, Enum):
    RESPONSIVE = "RESPONSIVE"
    SLEEPING_DOG = "SLEEPING_DOG"
    NEUTRAL = "NEUTRAL"


# P_HOLDOUT_ASSUMED matches EXPERIMENT.md so the demo makes sense numerically.
P_SELF_HEAL = 0.34          # untreated 7-day recovery rate
P_RESPONSIVE = 0.10         # fraction of customers in RESPONSIVE segment
P_SLEEPING_DOG = 0.05       # fraction of customers in SLEEPING_DOG segment
EFFECT_RESPONSIVE = 0.12    # +12 pp recovery for RESPONSIVE under treatment
EFFECT_SLEEPING_DOG = -0.04 #  -4 pp for SLEEPING_DOG (hurts them)


class _Truth:
    """Hidden ground-truth store. Not importable from outside this module."""

    def __init__(self) -> None:
        self._rng = _rng(1)
        self._segments: dict[str, CustomerSegment] = {}
        self._self_heal: dict[str, bool] = {}
        self._treatment_effect: dict[str, float] = {}

    def _ensure(self, customer_id: str) -> None:
        if customer_id not in self._segments:
            # Draw customer segment once per unique customer.
            roll = self._rng.random()
            if roll < P_SLEEPING_DOG:
                seg = CustomerSegment.SLEEPING_DOG
            elif roll < P_SLEEPING_DOG + P_RESPONSIVE:
                seg = CustomerSegment.RESPONSIVE
            else:
                seg = CustomerSegment.NEUTRAL

            # Draw self-heal flag (untreated outcome) once per customer.
            self_heal = self._rng.random() < P_SELF_HEAL

            # Treatment effect: how much does treatment change recovery probability?
            if seg is CustomerSegment.RESPONSIVE:
                effect = EFFECT_RESPONSIVE
            elif seg is CustomerSegment.SLEEPING_DOG:
                effect = EFFECT_SLEEPING_DOG
            else:
                effect = 0.0

            self._segments[customer_id] = seg
            self._self_heal[customer_id] = self_heal
            self._treatment_effect[customer_id] = effect

    def segment(self, customer_id: str) -> CustomerSegment:
        self._ensure(customer_id)
        return self._segments[customer_id]

    def self_healed(self, customer_id: str) -> bool:
        self._ensure(customer_id)
        return self._self_heal[customer_id]

    def treatment_recovery_prob(self, customer_id: str) -> float:
        """Probability of recovery IF a valid intervention is sent."""
        self._ensure(customer_id)
        return min(1.0, P_SELF_HEAL + self._treatment_effect[customer_id])

    def would_recover_under_treatment(self, customer_id: str) -> bool:
        self._ensure(customer_id)
        prob = self.treatment_recovery_prob(customer_id)
        return self._rng.random() < prob


# Singleton hidden ground truth.
_truth = _Truth()


# ------------------------------------------------------------------------- simulator case

@dataclass
class SimulatorCase:
    """A synthetic case produced by the simulator.

    The `outcome_*` fields are set by the simulator after the case is processed.
    `outcome_untouched` is the ground truth: what would have happened with no
    intervention. `outcome_treated` is what happened under the arm's treatment.
    `outcome_treated is None` until `simulate_treatment` is called.
    """
    case_id: str
    customer_id: str
    case_type: str
    ticket_amount_paise: int
    error_reason: Optional[str]
    error_source: Optional[str]
    error_step: Optional[str]
    error_description: Optional[str]
    order_id: str = ""
    payment_id: str = ""

    # Ground truth (set by Simulator._finalize)
    outcome_untouched: Optional[bool] = None   # None = unknown
    outcome_treated: Optional[bool] = None     # None = no treatment attempted
    segment: Optional[CustomerSegment] = None


# ------------------------------------------------------------------------- simulator

# Realistic error-reason distribution (matches Razorpay failure taxonomy).
_ERROR_REASONS = [
    ("CARD_DECLINED",               0.30),
    ("INSUFFICIENT_BALANCES",       0.20),
    ("NETWORK_ERROR",               0.18),
    ("PAYMENT_AUTHENTICATION_FAILED", 0.15),
    ("BANK_DECLINED",               0.10),
    ("BAD_REQUEST_ERROR",           0.05),
    (None,                          0.02),   # no structured error — free-text only
]

_ERROR_SOURCES = [
    ("customer", 0.50),
    ("bank",     0.30),
    (None,       0.20),
]

_ERROR_STEPS = [
    ("payment_authentication", 0.40),
    (None,                    0.60),
]


def _weighted_choice(rng: random.Random, choices: list[tuple[str | None, float]]) -> str | None:
    total = sum(w for _, w in choices)
    roll = rng.random() * total
    cumulative = 0.0
    for value, weight in choices:
        cumulative += weight
        if roll < cumulative:
            return value
    return choices[-1][0]


@dataclass
class Simulator:
    """Seeded synthetic-payment-failure simulator.

    Args:
        seed: Additional seed added to config.SEED. Allows multiple independent
              simulation runs without changing the master seed.
        max_ticket_paise: Upper bound on generated ticket amounts.
    """
    seed: int = 0
    max_ticket_paise: int = 500_000  # ₹5,000 max
    _case_counter: int = field(default=0, init=False, repr=False)
    _rng: random.Random = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = _rng(self.seed)
        self._case_counter = 0

    # ------------------------------------------------------------------ generation

    def generate_case(
        self,
        case_id: str | None = None,
        customer_id: str | None = None,
        case_type: str | None = None,
    ) -> SimulatorCase:
        """Generate one deterministic synthetic case.

        Unspecified arguments are drawn from the seeded RNG, so the same call
        order always produces the same case.
        """
        self._case_counter += 1
        cid = case_id or f"sim_{self._case_counter:06d}"
        cust = customer_id or f"cust_{self._rng.randint(1, 500):06d}"
        ctype = case_type or ("upi_autopay" if self._rng.random() < 0.35 else "one_time_link")

        error_reason = _weighted_choice(self._rng, _ERROR_REASONS)
        error_source = _weighted_choice(self._rng, _ERROR_SOURCES)
        error_step = _weighted_choice(self._rng, _ERROR_STEPS)

        # Log-normal-ish amount centred around ₹250 (25,000 paise).
        # exp(rng.gauss(10, 1.5)) gives a realistic payment amount distribution.
        raw_amount = int(self._rng.expovariate(1 / 25000))
        amount = min(raw_amount, self.max_ticket_paise)
        if amount < 500:   # minimum ₹5
            amount = 500

        order_id = f"order_{self._rng.getrandbits(64):016x}"
        payment_id = f"pay_{self._rng.getrandbits(64):016x}"

        return SimulatorCase(
            case_id=cid,
            customer_id=cust,
            case_type=ctype,
            ticket_amount_paise=amount,
            error_reason=error_reason,
            error_source=error_source,
            error_step=error_step,
            error_description=None,
            order_id=order_id,
            payment_id=payment_id,
        )

    def generate_batch(self, n: int) -> list[SimulatorCase]:
        """Generate ``n`` deterministic synthetic cases."""
        return [self.generate_case() for _ in range(n)]

    # ------------------------------------------------------------------ ground truth

    def ground_truth_for(self, case: SimulatorCase) -> tuple[bool, CustomerSegment]:
        """Return (self_healed, segment) for this case's customer.

        Segment is drawn once per unique customer_id and cached in _truth.
        """
        segment = _truth.segment(case.customer_id)
        self_healed = _truth.self_healed(case.customer_id)
        return self_healed, segment

    def simulate_treatment(self, case: SimulatorCase) -> bool:
        """Draw the treated outcome for this case's customer.

        Sets ``case.outcome_untouched`` (if not already set) and
        ``case.outcome_treated`` to the drawn value, and returns the treated
        outcome. Call this after the case has been processed by the decision
        policy to record what actually happened.

        Idempotent: if ``outcome_treated`` is already set, returns it without
        re-drawing. This makes repeated calls safe for the same case.
        """
        # Idempotent: if already drawn, return the cached value.
        if case.outcome_treated is not None:
            return case.outcome_treated

        # Untouched outcome (ground truth): did they recover without us?
        if case.outcome_untouched is None:
            case.outcome_untouched = _truth.self_healed(case.customer_id)

        case.segment = _truth.segment(case.customer_id)

        # Treated outcome: did they recover after our intervention?
        case.outcome_treated = _truth.would_recover_under_treatment(case.customer_id)
        return case.outcome_treated

    # ------------------------------------------------------------------ failure injections

    def simulate_duplicate_webhook(
        self,
        conn: sqlite3.Connection,
        case,  # Case | SimulatorCase
        proposal: Proposal,
        verdict: Verdict,
        caller=None,  # type: Callable | None
    ) -> list[dict]:
        """Inject three duplicate webhook deliveries for the same intent.

        The first delivery creates the intent and calls the injected ``caller``.
        The second and third deliveries find the existing idempotency key and return
        the existing intent without calling the provider. All three deliveries use
        ``attempt=1``; they differ only in delivery count.

        Args:
            caller: the provider callable. Defaults to a simple stub returning
                ``{"id": "fake_ref_<case_id>"}`` if not provided.

        Returns a list of three dicts with ``delivery``, ``intent_id``,
        ``status``, and ``idempotency_key`` keys.
        """
        results: list[dict] = []

        def default_caller(request):
            return {"id": f"fake_ref_{request.case_id}"}

        active_caller = caller if caller is not None else default_caller

        for delivery in (1, 2, 3):
            # All three deliveries share the same attempt number. The first call
            # creates the intent; the second and third find the existing key and
            # are suppressed before the provider is called.
            intent = act_execute(
                conn,
                case,
                proposal,
                verdict,
                active_caller,
                attempt=1,
            )
            results.append({
                "delivery": delivery,
                "intent_id": intent.intent_id,
                "status": intent.status.value,
                "idempotency_key": intent.idempotency_key,
            })

        return results

    def simulate_order_paid(
        self,
        conn: sqlite3.Connection,
        case,  # Case | SimulatorCase
    ) -> list[dict]:
        """Inject an order.paid event arriving mid-intervention.

        Cancels all PENDING or UNKNOWN intents for the case's order via
        ``act.cancel_pending``. Returns a list describing each cancelled intent.
        """
        order_id = getattr(case, "order_id", None) or ""
        cancelled = cancel_pending(conn, order_id, reason="order.paid")
        return [
            {
                "intent_id": i.intent_id,
                "status_before": "PENDING",
                "status_after": i.status.value,
            }
            for i in cancelled
        ]

    def simulate_action_timeout(
        self,
        conn: sqlite3.Connection,
        case,  # Case | SimulatorCase
        proposal: Proposal,
        verdict: Verdict,
    ) -> dict:
        """Inject an action timeout by passing a caller that raises TimeoutError.

        Returns the resulting intent (status should be UNKNOWN).
        """
        def timing_out_caller(request):
            raise TimeoutError("simulated network timeout")

        intent = act_execute(
            conn,
            case,
            proposal,
            verdict,
            timing_out_caller,
        )
        return {
            "intent_id": intent.intent_id,
            "status": intent.status.value,
            "is_unknown": intent.status is IntentStatus.UNKNOWN,
            "is_not_failed": intent.status is not IntentStatus.FAILED,
        }
