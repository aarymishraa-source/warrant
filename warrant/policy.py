"""
The policy engine. This module is the authority; the model is not.

Everything a language model produces arrives here as a `Proposal` -- a request,
never an instruction. `evaluate()` may refuse it, and nothing in a proposal can
lift a gate, widen a cap, or change a number. The proposal contributes exactly
one input to the arithmetic below: a confidence score, which can only ever cause
a refusal (gate `low_confidence`), never an approval.

**The LLM never computes expected value.** EV is computed here, from
`p_uplift * case.ticket_amount_paise`, in integer paise, against a cost read
from `ACTION_COST_INR`. A model that could produce the EV could also produce the
EV that justifies the action it already wanted -- which is the failure this whole
project exists to argue against. `p_uplift` is an estimate of *incremental*
recovery probability and may be negative: a customer who would have paid anyway
is not worth spending on, and a sleeping-dog segment is worth less than nothing.

Product policy vs regulation
----------------------------
Exactly one gate here is regulatory: `npci_autopay_cap`, which enforces NPCI's
one-execution-plus-three-retries limit and fires ONLY on `upi_autopay` cases.
Every other gate -- attempt cap, frequency cap, spend ceiling, EV threshold --
is OUR product policy, chosen by us, arguable, and labelled as such in each
gate's docstring. Presenting a product choice as a legal requirement is the
over-claim recorded as F-002 in FAILURES.md.

Gate order is data, not control flow: `GATES` is an ordered tuple and the first
gate to fire wins. Order matters and is asserted in the tests.

Every verdict -- refusal or approval -- writes exactly one `audit_log` row
carrying rule_id, decision, EV and cost. A verdict that cannot be explained
after the fact is a bug, not a decision.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from warrant.assign import HOLDOUT, Assignment
from warrant.config import (
    ACTION_COST_INR,
    COHORT_SPEND_CEILING_INR,
    EV_MARGIN_INR,
    FREQUENCY_CAP_PER_CUSTOMER_7D,
    LLM_CONFIDENCE_FLOOR,
    MAX_ATTEMPTS_PER_CASE,
    NPCI_AUTOPAY_MAX_ATTEMPTS,
)
from warrant.core import Case, CaseState, is_terminal

AUTOPAY_CASE_TYPE = "upi_autopay"

FREQUENCY_WINDOW_DAYS = 7
"""The "7D" in FREQUENCY_CAP_PER_CUSTOMER_7D. Product policy, not regulation --
see F-002: the RBI contact-window rule governs digital lending, not dunning."""

# Rule ids. Stable strings: they land in audit_log and in the Day 10 UI, so
# renaming one silently rewrites history.
RULE_ARM_HOLDOUT = "arm_holdout"
RULE_TERMINAL_STATE = "terminal_state"
RULE_ATTEMPT_CAP = "attempt_cap"
RULE_NPCI_AUTOPAY_CAP = "npci_autopay_cap"
RULE_FREQUENCY_CAP = "frequency_cap"
RULE_COHORT_SPEND = "cohort_spend"
RULE_LOW_CONFIDENCE = "low_confidence"
RULE_UNKNOWN_ACTION = "unknown_action"
RULE_EV_THRESHOLD = "ev_threshold"
RULE_ALL_CLEAR = "all_gates_passed"


class Decision(str, Enum):
    EXECUTE = "EXECUTE"
    DO_NOTHING = "DO_NOTHING"
    DEFER = "DEFER"
    ESCALATE = "ESCALATE"


# DEFER and ESCALATE are declared but unreachable today: no gate emits them. They
# exist because Day 7 needs somewhere to put an execution whose outcome is
# UNKNOWN (a timeout is not a failure) without inventing vocabulary later. Do not
# read their presence as coverage.


@dataclass(frozen=True)
class Proposal:
    """What Day 8's proposer will hand us. A request, not an instruction.

    `confidence` is the model's own estimate of whether it should be trusted on
    this case. It can only lose the case an action (gate `low_confidence`); it
    can never win one, and it never enters the EV arithmetic.
    """
    action: str
    timing: str
    channel: str
    rationale: str
    confidence: float


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule_id: str
    reason: str
    expected_value_paise: int
    action_cost_paise: int | None
    evaluated_at: str


class UnassignedCaseError(Exception):
    """Raised when a case reaches the policy engine without an arm.

    Assignment happens before anything reads the case (see warrant/assign.py).
    Deciding on an unassigned case would put the decision outside the experiment
    and make its outcome unattributable, so it is refused rather than defaulted.
    """


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _audit(conn: sqlite3.Connection, action: str, case_id: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, event_id, detail_json) VALUES (?,?,?,?,?)",
        (_now(), "policy", action, None, json.dumps({"case_id": case_id, **detail}, sort_keys=True)),
    )


def _rupees(paise: int) -> str:
    """Render paise as rupees for a human-readable reason string.

    Whole rupees print without decimals -- "Rs 34", not "Rs 34.00" -- because
    these strings are read aloud in the demo and shown in the UI.
    """
    if paise % 100 == 0:
        return f"Rs {paise // 100}"
    return f"Rs {paise / 100:.2f}"


def _inr_to_paise(amount_inr: float) -> int:
    """Money is integer paise everywhere past this boundary. Floats are for the
    config file, which is written by humans in rupees."""
    return int(round(amount_inr * 100))


# ------------------------------------------------------------------- counters
# All three read from tables that already exist. The state machine is the record
# of what we did; nothing here keeps a second copy that could disagree with it.

def attempts_for_case(conn: sqlite3.Connection, case_id: str) -> int:
    """Attempts made on this case, counted as transitions INTO `action_queued`.

    Queued, not executed. An intent that was sent and timed out is UNKNOWN, never
    FAILED (NOTES.md) -- the money may well have moved -- so it has to count
    against the caps. Counting executions instead would let a string of timeouts
    retry forever.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM case_transitions WHERE case_id = ? AND to_state = ?",
        (case_id, CaseState.ACTION_QUEUED.value),
    ).fetchone()["n"]


def contacts_in_window(conn: sqlite3.Connection, customer_id: str,
                       days: int = FREQUENCY_WINDOW_DAYS) -> int:
    """Contacts to this CUSTOMER across all their cases in the trailing window.

    Per customer, not per case: a customer with three failed subscriptions is
    still one person receiving three messages.

    Timestamps are compared as ISO-8601 strings. That is only sound because
    every writer uses `_now()`, which is always UTC with the same offset format,
    so lexicographic order is chronological order.
    """
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
    return conn.execute(
        """SELECT COUNT(*) AS n
             FROM case_transitions t
             JOIN cases c ON c.case_id = t.case_id
            WHERE c.customer_id = ? AND t.to_state = ? AND t.at >= ?""",
        (customer_id, CaseState.ACTION_QUEUED.value, cutoff),
    ).fetchone()["n"]


def cohort_spend_paise(conn: sqlite3.Connection) -> int:
    """Spend this engine has already committed, summed from its own audit trail.

    Every EXECUTE verdict records the cost it authorised, so the audit log is the
    spend ledger until Day 7's intent ledger exists. It is deliberately an
    over-count of money actually moved: an authorised action that never left the
    building still consumed budget as far as this gate is concerned, which is the
    conservative direction for a ceiling.
    """
    rows = conn.execute(
        "SELECT detail_json FROM audit_log WHERE actor = 'policy' AND action = 'verdict_execute'"
    ).fetchall()
    return sum(json.loads(r["detail_json"]).get("action_cost_paise") or 0 for r in rows)


# --------------------------------------------------------------------- gates
# Each gate returns a reason string when it REFUSES, or None when it passes.
# A gate never approves anything; approval is only the absence of every refusal.

@dataclass(frozen=True)
class GateContext:
    conn: sqlite3.Connection
    case: Case
    assignment: Assignment
    proposal: Proposal
    p_uplift: float
    expected_value_paise: int
    action_cost_paise: int | None
    attempts: int
    contacts_7d: int
    committed_spend_paise: int


def gate_arm_holdout(ctx: GateContext) -> str | None:
    """HOLDOUT means untreated. No exceptions, no override, first in the order.

    Experiment integrity. A holdout that gets treated "just this once, the
    proposal was excellent" is not a holdout, and the primary comparison
    (EXPERIMENT.md) silently becomes a measurement of nothing. Carved-out cases
    never reach this gate because they are never assigned to HOLDOUT.
    """
    if ctx.assignment.arm == HOLDOUT:
        return "case is in the HOLDOUT arm and is never treated"
    return None


def gate_terminal_state(ctx: GateContext) -> str | None:
    """A settled case is not a candidate. Product policy.

    Acting on a case that already resolved externally is how a recovery system
    ends up billing for a payment it did not cause.
    """
    if is_terminal(ctx.case.state):
        return f"case already terminal in {ctx.case.state.value}"
    return None


def gate_attempt_cap(ctx: GateContext) -> str | None:
    """At most MAX_ATTEMPTS_PER_CASE interventions per case. PRODUCT POLICY.

    Ours, arguable, and stricter than any regulation we are subject to. Note it
    is stricter than the NPCI cap too -- see `gate_npci_autopay_cap`.
    """
    if ctx.attempts >= MAX_ATTEMPTS_PER_CASE:
        return (f"attempt cap reached: {ctx.attempts} of "
                f"{MAX_ATTEMPTS_PER_CASE} attempts already made")
    return None


def gate_npci_autopay_cap(ctx: GateContext) -> str | None:
    """The ONLY regulatory gate in this module. VERIFIED, and narrowly scoped.

    NPCI, effective August 2025: a UPI Autopay mandate permits one execution plus
    three retries per cycle, four in total. This fires ONLY on
    `case_type == 'upi_autopay'`. It must never fire on a `one_time_link` case --
    a payment link is not a mandate execution, and applying a mandate rule to it
    would be exactly the kind of over-claim recorded as F-002.

    Currently shadowed: MAX_ATTEMPTS_PER_CASE (3) is stricter than
    NPCI_AUTOPAY_MAX_ATTEMPTS (4) and `gate_attempt_cap` runs first, so in the
    shipped configuration this gate cannot be the one that fires. It is kept, and
    tested directly, because product policy is ours to loosen and the regulation
    is not. See F-009.
    """
    if ctx.case.case_type != AUTOPAY_CASE_TYPE:
        return None
    if ctx.attempts >= NPCI_AUTOPAY_MAX_ATTEMPTS:
        return (f"NPCI autopay cap reached: {ctx.attempts} of "
                f"{NPCI_AUTOPAY_MAX_ATTEMPTS} permitted attempts in this mandate cycle")
    return None


def gate_frequency_cap(ctx: GateContext) -> str | None:
    """At most FREQUENCY_CAP_PER_CUSTOMER_7D contacts per customer per 7 days.

    PRODUCT POLICY, not regulation (F-002). The gate asks whether THIS contact
    would breach the cap, so it refuses once the customer already has that many
    contacts in the window -- a cap of 2 permits a second message, not a third.
    """
    if ctx.contacts_7d >= FREQUENCY_CAP_PER_CUSTOMER_7D:
        return (f"frequency cap reached: {ctx.contacts_7d} contacts to this customer "
                f"in the trailing {FREQUENCY_WINDOW_DAYS} days, cap is "
                f"{FREQUENCY_CAP_PER_CUSTOMER_7D}")
    return None


def gate_cohort_spend(ctx: GateContext) -> str | None:
    """The cohort's total authorised spend may not exceed the ceiling. PRODUCT POLICY.

    Evaluated as "would this action take us over", not "are we already over", so
    the ceiling is never crossed rather than detected after the fact.
    """
    if ctx.action_cost_paise is None:
        return None  # unknown action; gate_unknown_action refuses it first
    ceiling = _inr_to_paise(COHORT_SPEND_CEILING_INR)
    projected = ctx.committed_spend_paise + ctx.action_cost_paise
    if projected > ceiling:
        return (f"cohort spend ceiling: {_rupees(projected)} would exceed "
                f"{_rupees(ceiling)}")
    return None


def gate_low_confidence(ctx: GateContext) -> str | None:
    """The proposer abstains below LLM_CONFIDENCE_FLOOR. PRODUCT POLICY.

    An uncertain model is not a cheap model: acting on a coin-flip proposal costs
    real money and contaminates the arm. Note the asymmetry -- high confidence
    buys nothing, it merely fails to disqualify.
    """
    if ctx.proposal.confidence < LLM_CONFIDENCE_FLOOR:
        return (f"proposer abstained: confidence {ctx.proposal.confidence:.2f} "
                f"below floor {LLM_CONFIDENCE_FLOOR:.2f}")
    return None


def gate_unknown_action(ctx: GateContext) -> str | None:
    """The action must be one we have priced. PRODUCT POLICY, and a safety rail.

    A model that invents an action name -- or is talked into one -- gets a
    refusal, not an execution. Malformed output routes to DO_NOTHING and is
    audited (NOTES.md, the AI boundary).
    """
    if ctx.proposal.action not in ACTION_COST_INR:
        return f"action {ctx.proposal.action!r} is not in the priced action set"
    return None


def gate_ev_threshold(ctx: GateContext) -> str | None:
    """The centrepiece. Intervening must clear its own cost. PRODUCT POLICY.

    `expected_value = p_uplift * ticket_amount_paise`, computed here and nowhere
    else. `p_uplift` is INCREMENTAL: the probability the action changes the
    outcome, not the probability the customer pays. A customer who was going to
    pay on day 3 anyway carries an EV near zero however large their ticket, and a
    sleeping-dog segment carries a negative one.

    The comparison is `<=`, not `<`: an action that exactly breaks even is not
    worth taking, and DO_NOTHING is a scored economic decision rather than a
    fallback.
    """
    if ctx.action_cost_paise is None:
        return None  # unknown action; gate_unknown_action refuses it first
    margin = _inr_to_paise(EV_MARGIN_INR)
    if ctx.expected_value_paise <= ctx.action_cost_paise + margin:
        reason = (f"expected incremental value {_rupees(ctx.expected_value_paise)} "
                  f"does not clear action cost {_rupees(ctx.action_cost_paise)}")
        if margin:
            reason += f" plus margin {_rupees(margin)}"
        return reason
    return None


@dataclass(frozen=True)
class Gate:
    rule_id: str
    check: object  # Callable[[GateContext], str | None]
    decision: Decision = Decision.DO_NOTHING


GATES: tuple[Gate, ...] = (
    Gate(RULE_ARM_HOLDOUT, gate_arm_holdout),
    Gate(RULE_TERMINAL_STATE, gate_terminal_state),
    Gate(RULE_ATTEMPT_CAP, gate_attempt_cap),
    Gate(RULE_NPCI_AUTOPAY_CAP, gate_npci_autopay_cap),
    Gate(RULE_FREQUENCY_CAP, gate_frequency_cap),
    Gate(RULE_COHORT_SPEND, gate_cohort_spend),
    Gate(RULE_LOW_CONFIDENCE, gate_low_confidence),
    Gate(RULE_UNKNOWN_ACTION, gate_unknown_action),
    Gate(RULE_EV_THRESHOLD, gate_ev_threshold),
)


def expected_value_paise(p_uplift: float, ticket_amount_paise: int) -> int:
    """EV in integer paise. Computed here, never by a model.

    Rounded half-to-even by `round()`; sub-paise precision on an estimate whose
    input is a DESIGN assumption would be false precision.
    """
    return int(round(p_uplift * ticket_amount_paise))


def action_cost_paise(action: str) -> int | None:
    """Priced cost of an action, or None if we have never priced it."""
    cost = ACTION_COST_INR.get(action)
    return None if cost is None else _inr_to_paise(cost)


def evaluate(
    conn: sqlite3.Connection,
    case: Case,
    assignment: Assignment,
    proposal: Proposal,
    p_uplift: float,
) -> Verdict:
    """Run every gate in order and return the first refusal, or EXECUTE.

    The proposal is an input to this function, never an authority over it. No
    branch below consults it for anything except its action name and its
    confidence, and neither can approve a case on its own.

    Raises `UnassignedCaseError` if the case has no arm, and `ValueError` if
    `p_uplift` is outside [-1, 1]. Writes exactly one audit row either way --
    including for those refusals, because a decision we cannot explain later is
    a bug.
    """
    if assignment is None:
        raise UnassignedCaseError(
            f"case {case.case_id} reached the policy engine without an arm"
        )
    if assignment.case_id != case.case_id:
        raise ValueError(
            f"assignment is for case {assignment.case_id}, not {case.case_id}"
        )
    if not -1.0 <= p_uplift <= 1.0:
        # Negative is legitimate (sleeping dogs, CALIBRATION.md); impossible is not.
        raise ValueError(f"p_uplift {p_uplift} is outside [-1, 1]")

    ev = expected_value_paise(p_uplift, case.ticket_amount_paise)
    cost = action_cost_paise(proposal.action)

    ctx = GateContext(
        conn=conn,
        case=case,
        assignment=assignment,
        proposal=proposal,
        p_uplift=p_uplift,
        expected_value_paise=ev,
        action_cost_paise=cost,
        attempts=attempts_for_case(conn, case.case_id),
        contacts_7d=contacts_in_window(conn, case.customer_id),
        committed_spend_paise=cohort_spend_paise(conn),
    )

    for gate in GATES:
        reason = gate.check(ctx)
        if reason is not None:
            return _record(conn, ctx, gate.decision, gate.rule_id, reason)

    return _record(
        conn, ctx, Decision.EXECUTE, RULE_ALL_CLEAR,
        f"expected incremental value {_rupees(ev)} clears action cost "
        f"{_rupees(cost)} and every gate passed",
    )


def _record(conn: sqlite3.Connection, ctx: GateContext, decision: Decision,
            rule_id: str, reason: str) -> Verdict:
    """Build the verdict and write the single audit row that explains it."""
    verdict = Verdict(
        decision=decision,
        rule_id=rule_id,
        reason=reason,
        expected_value_paise=ctx.expected_value_paise,
        action_cost_paise=ctx.action_cost_paise,
        evaluated_at=_now(),
    )
    _audit(conn, f"verdict_{decision.value.lower()}", ctx.case.case_id, {
        "rule_id": rule_id,
        "decision": decision.value,
        "reason": reason,
        "expected_value_paise": verdict.expected_value_paise,
        "action_cost_paise": verdict.action_cost_paise,
        "arm": ctx.assignment.arm,
        "proposed_action": ctx.proposal.action,
        "p_uplift": ctx.p_uplift,
        "attempts": ctx.attempts,
        "contacts_7d": ctx.contacts_7d,
    })
    conn.commit()
    return verdict
