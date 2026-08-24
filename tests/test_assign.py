"""
Day 5 assignment tests. In-memory SQLite only, no network.

Two things are being defended here. First, determinism: a customer's arm is a
function of their id and the salt, so the same customer cannot drift between
arms across their own cases. Second, the carve-out rule: cases that a real
merchant cannot hold out are still assigned, still measured, and never land in
HOLDOUT.
"""
from __future__ import annotations

import json
from collections import Counter

import pytest

from warrant import db
from warrant.assign import (
    CARVE_CASE_TYPE,
    CARVE_HIGH_VALUE,
    HOLDOUT,
    AlreadyAssignedError,
    arm_for_customer,
    assign_case,
    carve_reasons,
    get_assignment,
)
from warrant.config import ARM_WEIGHTS, ARMS, HIGH_VALUE_THRESHOLD_PAISE
from warrant.core import create_case

ORDINARY_TICKET_PAISE = 34000

# A customer whose untreated draw IS HOLDOUT. The carve-out tests below are
# vacuous with any other fixture: "never HOLDOUT" passes trivially for a customer
# the hash was never going to put there. Verified by
# test_holdout_fixture_customer_really_is_holdout, which fails loudly if the salt
# or the weights move underneath it.
HOLDOUT_CUSTOMER = "cust_TESTFIXTURE003"
HIGH_VALUE_TICKET_PAISE = HIGH_VALUE_THRESHOLD_PAISE + 1

# 3 percentage points, as specified in the day plan.
BALANCE_TOLERANCE_PP = 3.0
BALANCE_N = 3000


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_case(conn, case_id: str, customer_id: str,
              case_type: str = "one_time_link",
              ticket_amount_paise: int = ORDINARY_TICKET_PAISE):
    return create_case(
        conn,
        case_id=case_id,
        customer_id=customer_id,
        case_type=case_type,
        ticket_amount_paise=ticket_amount_paise,
    )


def audit_actions(conn, case_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT action, detail_json FROM audit_log WHERE actor = 'assign' ORDER BY id"
    ).fetchall()
    return [r["action"] for r in rows if json.loads(r["detail_json"])["case_id"] == case_id]


# ------------------------------------------------------------- determinism

def test_same_customer_always_gets_the_same_arm():
    arms = {arm_for_customer("cust_TESTFIXTURE042") for _ in range(10)}
    assert len(arms) == 1


def test_same_customer_same_arm_across_their_own_cases(conn):
    """Randomising per case would leak treatment across a customer's own cases."""
    customer = "cust_TESTFIXTURE042"
    arms = set()
    for i in range(5):
        case = make_case(conn, f"case_multi_{i}", customer)
        arms.add(assign_case(conn, case).arm)
    assert len(arms) == 1


def test_arm_is_always_one_of_the_configured_arms():
    for i in range(200):
        assert arm_for_customer(f"cust_{i:05d}") in ARMS


# ----------------------------------------------------------------- balance

def test_arms_are_balanced_within_three_points():
    counts = Counter(arm_for_customer(f"cust_{i:05d}") for i in range(BALANCE_N))
    assert set(counts) == set(ARMS)
    for arm, weight in ARM_WEIGHTS.items():
        observed_pp = 100.0 * counts[arm] / BALANCE_N
        expected_pp = 100.0 * weight
        assert abs(observed_pp - expected_pp) <= BALANCE_TOLERANCE_PP, (
            f"{arm}: {observed_pp:.2f}pp observed vs {expected_pp:.2f}pp configured"
        )


# --------------------------------------------------------------- carve-outs

def test_holdout_fixture_customer_really_is_holdout():
    """Guards the carve-out tests from going quietly vacuous."""
    assert arm_for_customer(HOLDOUT_CUSTOMER) == HOLDOUT


def test_high_value_case_is_carved_out_and_never_holdout(conn):
    case = make_case(conn, "case_high_value", HOLDOUT_CUSTOMER,
                     ticket_amount_paise=HIGH_VALUE_TICKET_PAISE)
    assignment = assign_case(conn, case)
    assert assignment.carved_out is True
    assert assignment.arm != HOLDOUT


def test_lending_emi_case_is_carved_out_and_never_holdout(conn):
    case = make_case(conn, "case_lending", HOLDOUT_CUSTOMER,
                     case_type="lending_emi")
    assignment = assign_case(conn, case)
    assert assignment.carved_out is True
    assert assignment.arm != HOLDOUT


def test_no_carved_out_customer_lands_in_holdout(conn):
    """Swept across many customers, not just one lucky hash."""
    for i in range(500):
        case = make_case(conn, f"case_sweep_{i}", f"cust_{i:05d}",
                         ticket_amount_paise=HIGH_VALUE_TICKET_PAISE)
        assert assign_case(conn, case).arm != HOLDOUT


def test_ordinary_case_is_not_carved_out(conn):
    case = make_case(conn, "case_ordinary", "cust_TESTFIXTURE042")
    assignment = assign_case(conn, case)
    assert assignment.carved_out is False
    assert assignment.carve_reason is None


def test_threshold_is_strictly_above(conn):
    """A ticket exactly at the threshold is not yet high value."""
    case = make_case(conn, "case_at_threshold", "cust_TESTFIXTURE042",
                     ticket_amount_paise=HIGH_VALUE_THRESHOLD_PAISE)
    assert carve_reasons(case) == ()


def test_carve_reason_is_recorded(conn):
    case = make_case(conn, "case_reason", "cust_TESTFIXTURE042",
                     ticket_amount_paise=HIGH_VALUE_TICKET_PAISE)
    assign_case(conn, case)
    stored = get_assignment(conn, "case_reason")
    assert stored is not None
    assert stored.carve_reason == CARVE_HIGH_VALUE
    assert stored.carved_out is True


def test_both_carve_reasons_recorded_when_both_apply(conn):
    case = make_case(conn, "case_both", "cust_TESTFIXTURE042",
                     case_type="lending_emi",
                     ticket_amount_paise=HIGH_VALUE_TICKET_PAISE)
    assignment = assign_case(conn, case)
    assert assignment.carve_reason == f"{CARVE_HIGH_VALUE},{CARVE_CASE_TYPE}"


# ------------------------------------------------------------------- audit

def test_carve_out_is_evaluated_and_logged_before_assignment(conn):
    case = make_case(conn, "case_audit", "cust_TESTFIXTURE042",
                     ticket_amount_paise=HIGH_VALUE_TICKET_PAISE)
    assign_case(conn, case)
    assert audit_actions(conn, "case_audit") == ["carve_out_evaluated", "arm_assigned"]


def test_every_assignment_is_audited(conn):
    case = make_case(conn, "case_plain_audit", "cust_TESTFIXTURE042")
    assignment = assign_case(conn, case)
    row = conn.execute(
        "SELECT detail_json FROM audit_log WHERE actor='assign' AND action='arm_assigned'"
    ).fetchone()
    detail = json.loads(row["detail_json"])
    assert detail["case_id"] == "case_plain_audit"
    assert detail["arm"] == assignment.arm


# ------------------------------------------------------------ persistence

def test_assignment_is_persisted_once_and_reused(conn):
    case = make_case(conn, "case_once", "cust_TESTFIXTURE042")
    first = assign_case(conn, case)
    second = assign_case(conn, case)
    assert second == first
    assert conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"] == 1
    assert audit_actions(conn, "case_once")[-1] == "assignment_reused"


def test_reassignment_to_a_different_arm_is_refused(conn):
    """Arms are fixed at assignment time; the ITT denominator depends on it."""
    case = make_case(conn, "case_frozen", "cust_TESTFIXTURE042")
    assign_case(conn, case)
    other = [a for a in ARMS if a != get_assignment(conn, "case_frozen").arm][0]
    conn.execute("UPDATE assignments SET arm = ? WHERE case_id = ?", (other, "case_frozen"))
    conn.commit()
    with pytest.raises(AlreadyAssignedError):
        assign_case(conn, case)
