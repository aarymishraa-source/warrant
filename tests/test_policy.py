"""
Day 6 policy tests. In-memory SQLite only, no network, no model.

Every gate gets a test that proves it REFUSES, and each of those tests fails if
its gate is removed from `GATES` -- verified by mutation, not assumed. The
mutation sweep lives in the FAILURES.md notes for F-009/F-010, not in the suite,
because a test that deletes production code is not a test.

Gate order is part of the contract: when two gates would both fire, the earlier
one owns the verdict.
"""
from __future__ import annotations

import json

import pytest

from warrant import db
from warrant.assign import Assignment
from warrant.config import (
    COHORT_SPEND_CEILING_INR,
    FREQUENCY_CAP_PER_CUSTOMER_7D,
    LLM_CONFIDENCE_FLOOR,
    MAX_ATTEMPTS_PER_CASE,
    NPCI_AUTOPAY_MAX_ATTEMPTS,
)
from warrant.core import CaseState, create_case, fetch_case, transition
from warrant.policy import (
    GATES,
    RULE_ALL_CLEAR,
    RULE_ARM_HOLDOUT,
    RULE_ATTEMPT_CAP,
    RULE_COHORT_SPEND,
    RULE_EV_THRESHOLD,
    RULE_FREQUENCY_CAP,
    RULE_LOW_CONFIDENCE,
    RULE_NPCI_AUTOPAY_CAP,
    RULE_TERMINAL_STATE,
    RULE_UNKNOWN_ACTION,
    Decision,
    Proposal,
    UnassignedCaseError,
    attempts_for_case,
    cohort_spend_paise,
    evaluate,
)

# The centrepiece fixture, from NOTES.md's second demo moment: a Rs 340 ticket
# with a 10% incremental uplift is worth Rs 34, and a payment link costs Rs 55.
CENTREPIECE_TICKET_PAISE = 34_000   # Rs 340
CENTREPIECE_P_UPLIFT = 0.10         # -> EV Rs 34
LINK_COST_PAISE = 5_500             # Rs 55

CUSTOMER = "cust_TESTFIXTURE001"

# A proposal that is not the reason anything gets refused: well-formed, priced
# action, confidence comfortably above the floor.
GOOD_PROPOSAL = Proposal(
    action="SEND_PAYMENT_LINK",
    timing="immediate",
    channel="sms",
    rationale="card declined at authentication; a fresh link usually clears it",
    confidence=0.90,
)


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_case(conn, case_id: str = "case_TESTFIXTURE001", customer_id: str = CUSTOMER,
              case_type: str = "one_time_link",
              ticket_amount_paise: int = CENTREPIECE_TICKET_PAISE):
    return create_case(
        conn,
        case_id=case_id,
        customer_id=customer_id,
        case_type=case_type,
        ticket_amount_paise=ticket_amount_paise,
    )


def make_assignment(case, arm: str = "WARRANT") -> Assignment:
    """Built directly rather than drawn: these tests are about gates, and the
    hash would otherwise decide which arm each fixture lands in."""
    return Assignment(
        case_id=case.case_id,
        customer_id=case.customer_id,
        arm=arm,
        carved_out=False,
        carve_reason=None,
        assigned_at="2026-08-24T00:00:00+00:00",
    )


def queue_attempts(conn, case_id: str, n: int):
    """Drive a case through n queue/execute cycles, the way Day 7 will.

    Attempts are counted as transitions into `action_queued`, so this is what an
    n-attempt case actually looks like in the transition log.
    """
    case = fetch_case(conn, case_id)
    case = transition(conn, case_id, CaseState.CLASSIFIED, case.version, reason="fixture")
    case = transition(conn, case_id, CaseState.ASSIGNED, case.version, reason="fixture")
    for i in range(n):
        case = transition(conn, case_id, CaseState.ACTION_QUEUED, case.version,
                          reason=f"attempt_{i + 1}")
        if i < n - 1:
            case = transition(conn, case_id, CaseState.ACTION_EXECUTED, case.version,
                              reason=f"attempt_{i + 1}_sent")
    return case


def policy_audit_rows(conn, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT action, detail_json FROM audit_log WHERE actor = 'policy' ORDER BY id"
    ).fetchall()
    return [json.loads(r["detail_json"]) for r in rows
            if json.loads(r["detail_json"])["case_id"] == case_id]


# ------------------------------------------------------------- gate 1: holdout

def test_holdout_arm_is_never_treated(conn):
    """Even a perfect proposal on a case where the numbers obviously work."""
    case = make_case(conn, ticket_amount_paise=10_000_000)  # Rs 100,000 ticket
    verdict = evaluate(conn, case, make_assignment(case, arm="HOLDOUT"),
                       GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_ARM_HOLDOUT


def test_same_case_in_a_treatment_arm_would_execute(conn):
    """Proves the holdout test above is about the arm and nothing else."""
    case = make_case(conn, ticket_amount_paise=10_000_000)
    verdict = evaluate(conn, case, make_assignment(case, arm="WARRANT"),
                       GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.EXECUTE


# ------------------------------------------------------- gate 2: terminal state

def test_terminal_case_is_refused(conn):
    case = make_case(conn)
    case = transition(conn, case.case_id, CaseState.RESOLVED_EXTERNALLY, case.version,
                      reason="order.paid")
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_TERMINAL_STATE


# ---------------------------------------------------------- gate 3: attempt cap

def test_attempt_cap_refuses_at_the_cap(conn):
    case = make_case(conn)
    queue_attempts(conn, case.case_id, MAX_ATTEMPTS_PER_CASE)
    case = fetch_case(conn, case.case_id)
    assert attempts_for_case(conn, case.case_id) == MAX_ATTEMPTS_PER_CASE
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_ATTEMPT_CAP


def test_a_case_below_every_cap_still_executes(conn):
    case = make_case(conn)
    queue_attempts(conn, case.case_id, 1)
    case = fetch_case(conn, case.case_id)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.EXECUTE


def test_frequency_cap_shadows_the_attempt_cap_inside_the_window(conn):
    """A case's own attempts are also contacts to its customer, and the customer
    cap (2) is stricter than the per-case cap (3). So a case that burns three
    attempts inside one week is stopped at two, by `frequency_cap` -- the
    per-case cap only owns the refusal when attempts are spread across more than
    the trailing window. Pinned because it is a config interaction, not a bug,
    and the two caps are easy to read as independent. See F-010."""
    case = make_case(conn)
    queue_attempts(conn, case.case_id, MAX_ATTEMPTS_PER_CASE - 1)
    case = fetch_case(conn, case.case_id)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_FREQUENCY_CAP


# ------------------------------------------------------------- gate 4: NPCI cap

def test_npci_cap_refuses_an_autopay_case_at_the_regulatory_limit(conn, monkeypatch):
    """The product attempt cap (3) is stricter than the NPCI cap (4) and runs
    first, so the product cap is lifted here to reach the regulatory gate at all.
    See F-009: in the shipped configuration this gate is shadowed."""
    monkeypatch.setattr("warrant.policy.MAX_ATTEMPTS_PER_CASE", 99)
    monkeypatch.setattr("warrant.policy.FREQUENCY_CAP_PER_CUSTOMER_7D", 99)
    case = make_case(conn, case_type="upi_autopay")
    queue_attempts(conn, case.case_id, NPCI_AUTOPAY_MAX_ATTEMPTS)
    case = fetch_case(conn, case.case_id)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_NPCI_AUTOPAY_CAP


def test_npci_cap_does_not_fire_on_a_one_time_link_at_four_attempts(conn, monkeypatch):
    """A payment link is not a mandate execution. Same attempt count, same
    lifted product caps, different case type -- and the gate must stay silent, so
    the case runs clean through every gate to EXECUTE."""
    monkeypatch.setattr("warrant.policy.MAX_ATTEMPTS_PER_CASE", 99)
    monkeypatch.setattr("warrant.policy.FREQUENCY_CAP_PER_CUSTOMER_7D", 99)
    case = make_case(conn, case_type="one_time_link")
    queue_attempts(conn, case.case_id, NPCI_AUTOPAY_MAX_ATTEMPTS)
    case = fetch_case(conn, case.case_id)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.rule_id != RULE_NPCI_AUTOPAY_CAP
    assert verdict.decision is Decision.EXECUTE


def test_npci_cap_does_not_fire_on_a_one_time_link_under_the_shipped_config(conn):
    """The same scoping check without monkeypatching anything: with the real
    caps the product gate owns the refusal, and it must not be the NPCI one."""
    case = make_case(conn, case_type="one_time_link")
    queue_attempts(conn, case.case_id, NPCI_AUTOPAY_MAX_ATTEMPTS)
    case = fetch_case(conn, case.case_id)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.rule_id == RULE_ATTEMPT_CAP


# -------------------------------------------------------- gate 5: frequency cap

def test_frequency_cap_counts_across_a_customers_cases(conn):
    """One customer, three failed payments: still one person being messaged."""
    for i in range(FREQUENCY_CAP_PER_CUSTOMER_7D):
        other = make_case(conn, case_id=f"case_earlier_{i}")
        queue_attempts(conn, other.case_id, 1)

    case = make_case(conn, case_id="case_third")
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_FREQUENCY_CAP


def test_frequency_cap_ignores_other_customers(conn):
    for i in range(FREQUENCY_CAP_PER_CUSTOMER_7D + 2):
        other = make_case(conn, case_id=f"case_other_{i}", customer_id=f"cust_other_{i}")
        queue_attempts(conn, other.case_id, 1)

    case = make_case(conn, case_id="case_mine")
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.EXECUTE


# -------------------------------------------------------- gate 6: cohort spend

def test_cohort_spend_ceiling_refuses_the_action_that_would_cross_it(conn):
    """Driven with the real ceiling rather than a patched one: authorise
    Rs 55 links until the next one would take the cohort over Rs 5,000."""
    ceiling_paise = int(round(COHORT_SPEND_CEILING_INR * 100))
    affordable = ceiling_paise // LINK_COST_PAISE

    for i in range(affordable):
        case = make_case(conn, case_id=f"case_spend_{i}", customer_id=f"cust_spend_{i}")
        verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
        assert verdict.decision is Decision.EXECUTE, f"case {i} refused: {verdict.reason}"

    assert cohort_spend_paise(conn) == affordable * LINK_COST_PAISE

    case = make_case(conn, case_id="case_over", customer_id="cust_over")
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_COHORT_SPEND


# ------------------------------------------------------- gate 7: low confidence

def test_low_confidence_proposal_is_refused(conn):
    case = make_case(conn)
    timid = Proposal(action="SEND_PAYMENT_LINK", timing="immediate", channel="sms",
                     rationale="not sure what went wrong here",
                     confidence=LLM_CONFIDENCE_FLOOR - 0.01)
    verdict = evaluate(conn, case, make_assignment(case), timid, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_LOW_CONFIDENCE


def test_confidence_exactly_at_the_floor_is_allowed(conn):
    case = make_case(conn)
    borderline = Proposal(action="SEND_PAYMENT_LINK", timing="immediate", channel="sms",
                          rationale="borderline", confidence=LLM_CONFIDENCE_FLOOR)
    verdict = evaluate(conn, case, make_assignment(case), borderline, p_uplift=0.9)
    assert verdict.decision is Decision.EXECUTE


def test_high_confidence_cannot_rescue_a_case_the_numbers_refuse(conn):
    """Confidence can only ever lose a case an action, never win it one."""
    case = make_case(conn)
    certain = Proposal(action="SEND_PAYMENT_LINK", timing="immediate", channel="sms",
                       rationale="absolutely certain this will work",
                       confidence=1.0)
    verdict = evaluate(conn, case, make_assignment(case), certain,
                       p_uplift=CENTREPIECE_P_UPLIFT)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_EV_THRESHOLD


# ------------------------------------------------------- gate 8: unknown action

def test_unpriced_action_is_refused(conn):
    """A model that invents an action gets a refusal, not an execution."""
    case = make_case(conn)
    invented = Proposal(action="CALL_CUSTOMER_AND_NEGOTIATE", timing="immediate",
                        channel="voice", rationale="seems reasonable", confidence=0.99)
    verdict = evaluate(conn, case, make_assignment(case), invented, p_uplift=0.9)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_UNKNOWN_ACTION
    assert verdict.action_cost_paise is None


# --------------------------------------------------------- gate 9: EV threshold

def test_centrepiece_ev_below_cost_is_refused(conn):
    """NOTES.md demo moment 2: EV Rs 34 does not clear a Rs 55 payment link."""
    case = make_case(conn, ticket_amount_paise=CENTREPIECE_TICKET_PAISE)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL,
                       p_uplift=CENTREPIECE_P_UPLIFT)
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_EV_THRESHOLD
    assert verdict.expected_value_paise == 3_400
    assert verdict.action_cost_paise == LINK_COST_PAISE
    assert "Rs 34" in verdict.reason
    assert "Rs 55" in verdict.reason
    assert verdict.reason == (
        "expected incremental value Rs 34 does not clear action cost Rs 55"
    )


def test_ev_clearing_cost_executes(conn):
    case = make_case(conn, ticket_amount_paise=CENTREPIECE_TICKET_PAISE)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.20)
    assert verdict.decision is Decision.EXECUTE
    assert verdict.rule_id == RULE_ALL_CLEAR
    assert verdict.expected_value_paise == 6_800


def test_exact_break_even_is_refused(conn):
    """`<=`, not `<`. Breaking even is not a reason to spend."""
    case = make_case(conn, ticket_amount_paise=LINK_COST_PAISE * 10)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.10)
    assert verdict.expected_value_paise == LINK_COST_PAISE
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_EV_THRESHOLD


def test_negative_uplift_is_refused(conn):
    """Sleeping dogs: a large ticket with negative incremental uplift is still
    a refusal, and the EV is negative rather than clamped."""
    case = make_case(conn, ticket_amount_paise=10_000_000)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=-0.04)
    assert verdict.expected_value_paise == -400_000
    assert verdict.decision is Decision.DO_NOTHING
    assert verdict.rule_id == RULE_EV_THRESHOLD


def test_impossible_uplift_is_rejected(conn):
    case = make_case(conn)
    with pytest.raises(ValueError):
        evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=1.4)


# ------------------------------------------------------------------ gate order

def test_gate_order_is_the_pre_registered_order():
    assert [g.rule_id for g in GATES] == [
        RULE_ARM_HOLDOUT,
        RULE_TERMINAL_STATE,
        RULE_ATTEMPT_CAP,
        RULE_NPCI_AUTOPAY_CAP,
        RULE_FREQUENCY_CAP,
        RULE_COHORT_SPEND,
        RULE_LOW_CONFIDENCE,
        RULE_UNKNOWN_ACTION,
        RULE_EV_THRESHOLD,
    ]


def test_earlier_gate_wins_when_several_would_fire(conn):
    """Holdout arm, terminal case, over the attempt cap, timid proposal, unpriced
    action and negative EV all at once: the first gate owns the verdict."""
    case = make_case(conn)
    queue_attempts(conn, case.case_id, MAX_ATTEMPTS_PER_CASE)
    case = fetch_case(conn, case.case_id)
    case = transition(conn, case.case_id, CaseState.RESOLVED_EXTERNALLY, case.version,
                      reason="order.paid")
    junk = Proposal(action="NOT_A_REAL_ACTION", timing="whenever", channel="carrier_pigeon",
                    rationale="", confidence=0.01)

    holdout_verdict = evaluate(conn, case, make_assignment(case, arm="HOLDOUT"),
                               junk, p_uplift=-0.5)
    assert holdout_verdict.rule_id == RULE_ARM_HOLDOUT

    # Drop the first gate's trigger and the next one takes over, in order.
    treated_verdict = evaluate(conn, case, make_assignment(case, arm="WARRANT"),
                               junk, p_uplift=-0.5)
    assert treated_verdict.rule_id == RULE_TERMINAL_STATE


def test_attempt_cap_outranks_low_confidence(conn):
    case = make_case(conn)
    queue_attempts(conn, case.case_id, MAX_ATTEMPTS_PER_CASE)
    case = fetch_case(conn, case.case_id)
    timid = Proposal(action="SEND_PAYMENT_LINK", timing="immediate", channel="sms",
                     rationale="unsure", confidence=0.01)
    verdict = evaluate(conn, case, make_assignment(case), timid, p_uplift=0.9)
    assert verdict.rule_id == RULE_ATTEMPT_CAP


# ----------------------------------------------------------------------- audit

def test_refusal_writes_exactly_one_audit_row(conn):
    case = make_case(conn)
    verdict = evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL,
                       p_uplift=CENTREPIECE_P_UPLIFT)
    rows = policy_audit_rows(conn, case.case_id)
    assert len(rows) == 1
    assert rows[0]["rule_id"] == verdict.rule_id
    assert rows[0]["decision"] == "DO_NOTHING"
    assert rows[0]["expected_value_paise"] == 3_400
    assert rows[0]["action_cost_paise"] == LINK_COST_PAISE


def test_execution_writes_exactly_one_audit_row(conn):
    case = make_case(conn)
    evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=0.9)
    rows = policy_audit_rows(conn, case.case_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "EXECUTE"
    assert rows[0]["rule_id"] == RULE_ALL_CLEAR


def test_every_verdict_is_explainable(conn):
    """One row per verdict, each carrying a rule_id, a reason, EV and cost."""
    case = make_case(conn)
    for p in (0.05, 0.5, 0.9):
        evaluate(conn, case, make_assignment(case), GOOD_PROPOSAL, p_uplift=p)
    rows = policy_audit_rows(conn, case.case_id)
    assert len(rows) == 3
    for row in rows:
        assert row["rule_id"]
        assert row["reason"]
        assert row["expected_value_paise"] is not None
        assert row["action_cost_paise"] == LINK_COST_PAISE


# ------------------------------------------------------------------ input guards

def test_unassigned_case_is_refused_outright(conn):
    """Assignment happens before anything reads the case; a case with no arm is
    a bug in the caller, not a case to decide."""
    case = make_case(conn)
    with pytest.raises(UnassignedCaseError):
        evaluate(conn, case, None, GOOD_PROPOSAL, p_uplift=0.9)


def test_assignment_for_a_different_case_is_refused(conn):
    case = make_case(conn)
    other = make_case(conn, case_id="case_someone_else", customer_id="cust_other")
    with pytest.raises(ValueError):
        evaluate(conn, case, make_assignment(other), GOOD_PROPOSAL, p_uplift=0.9)
