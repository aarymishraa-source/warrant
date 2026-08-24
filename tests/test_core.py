"""
Day 4 case state machine tests. In-memory SQLite only -- nothing here touches
the network, a real Razorpay account, or a file on disk.

The properties under test are the ones that break quietly in production: an
illegal move that half-applies, a lost update under concurrent delivery, a
transition that moves the row without leaving a record.
"""
from __future__ import annotations

import pytest

from warrant import db
from warrant.core import (
    ALLOWED,
    TERMINAL_STATES,
    CaseNotFoundError,
    CaseState,
    IllegalTransitionError,
    StaleVersionError,
    TerminalStateError,
    create_case,
    fetch_case,
    transition,
    transitions_for,
)

CASE_ID = "case_TESTFIXTURE001"
CUSTOMER_ID = "cust_TESTFIXTURE001"

# Shortest legal route from observed_failed to each non-terminal state.
ROUTES: dict[CaseState, tuple[CaseState, ...]] = {
    CaseState.OBSERVED_FAILED: (),
    CaseState.CLASSIFIED: (CaseState.CLASSIFIED,),
    CaseState.ASSIGNED: (CaseState.CLASSIFIED, CaseState.ASSIGNED),
    CaseState.ACTION_QUEUED: (
        CaseState.CLASSIFIED, CaseState.ASSIGNED, CaseState.ACTION_QUEUED,
    ),
    CaseState.ACTION_EXECUTED: (
        CaseState.CLASSIFIED, CaseState.ASSIGNED,
        CaseState.ACTION_QUEUED, CaseState.ACTION_EXECUTED,
    ),
}

NON_TERMINAL = [s for s in CaseState if s not in TERMINAL_STATES]


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def new_case(conn, case_id: str = CASE_ID, case_type: str = "one_time_link",
             ticket_amount_paise: int = 34000):
    return create_case(
        conn,
        case_id=case_id,
        customer_id=CUSTOMER_ID,
        case_type=case_type,
        ticket_amount_paise=ticket_amount_paise,
        payment_id="pay_TESTFIXTURE001",
        order_id="order_TESTFIXTURE001",
    )


def drive_to(conn, case_id: str, state: CaseState):
    """Walk a fresh case along ROUTES until it sits in `state`."""
    case = fetch_case(conn, case_id)
    for step in ROUTES[state]:
        case = transition(conn, case_id, step, case.version, reason="fixture")
    assert case.state is state
    return case


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# --------------------------------------------------------------------- setup

def test_new_case_starts_observed_failed_at_version_zero(conn):
    case = new_case(conn)
    assert case.state is CaseState.OBSERVED_FAILED
    assert case.version == 0
    assert count(conn, "case_transitions") == 0


def test_fetch_unknown_case_raises(conn):
    with pytest.raises(CaseNotFoundError):
        fetch_case(conn, "case_does_not_exist")


# ---------------------------------------------------------- legal transitions

def test_legal_transition_succeeds_and_bumps_version(conn):
    new_case(conn)
    moved = transition(conn, CASE_ID, CaseState.CLASSIFIED, 0, reason="root_cause_known")
    assert moved.state is CaseState.CLASSIFIED
    assert moved.version == 1
    assert fetch_case(conn, CASE_ID).state is CaseState.CLASSIFIED


def test_version_bumps_once_per_transition(conn):
    new_case(conn)
    case = drive_to(conn, CASE_ID, CaseState.ACTION_EXECUTED)
    assert case.version == len(ROUTES[CaseState.ACTION_EXECUTED]) == 4


def test_retry_cycle_is_legal(conn):
    """MAX_ATTEMPTS_PER_CASE is 3, so a second attempt has to be expressible.

    The cap itself is a policy gate (Day 6); the machine only permits the edge.
    """
    new_case(conn)
    case = drive_to(conn, CASE_ID, CaseState.ACTION_EXECUTED)
    case = transition(conn, CASE_ID, CaseState.ACTION_QUEUED, case.version, reason="attempt_2")
    assert case.state is CaseState.ACTION_QUEUED


# -------------------------------------------------------- illegal transitions

def test_illegal_transition_raises_and_changes_nothing(conn):
    new_case(conn)
    with pytest.raises(IllegalTransitionError):
        transition(conn, CASE_ID, CaseState.ACTION_EXECUTED, 0, reason="skipped_the_queue")
    case = fetch_case(conn, CASE_ID)
    assert case.state is CaseState.OBSERVED_FAILED
    assert case.version == 0
    assert count(conn, "case_transitions") == 0


def test_resolved_by_action_requires_an_executed_action(conn):
    """Credit for an action we never sent is the exact error this project exists
    to argue against."""
    new_case(conn)
    drive_to(conn, CASE_ID, CaseState.ACTION_QUEUED)
    case = fetch_case(conn, CASE_ID)
    with pytest.raises(IllegalTransitionError):
        transition(conn, CASE_ID, CaseState.RESOLVED_BY_ACTION, case.version)
    assert fetch_case(conn, CASE_ID).state is CaseState.ACTION_QUEUED


def test_backwards_transition_is_illegal(conn):
    new_case(conn)
    case = drive_to(conn, CASE_ID, CaseState.ASSIGNED)
    with pytest.raises(IllegalTransitionError):
        transition(conn, CASE_ID, CaseState.CLASSIFIED, case.version)
    assert fetch_case(conn, CASE_ID).state is CaseState.ASSIGNED


# ------------------------------------------------- payment state is not monotonic

@pytest.mark.parametrize("state", NON_TERMINAL, ids=lambda s: s.value)
def test_resolved_externally_reachable_from_every_non_terminal_state(conn, state):
    """order.paid can land at any moment. Every non-terminal state must accept it."""
    case_id = f"case_ext_{state.value}"
    new_case(conn, case_id=case_id)
    case = drive_to(conn, case_id, state)
    resolved = transition(
        conn, case_id, CaseState.RESOLVED_EXTERNALLY, case.version,
        reason="order.paid", event_id="evt_TESTFIXTURE_PAID",
    )
    assert resolved.state is CaseState.RESOLVED_EXTERNALLY
    assert resolved.version == case.version + 1


def test_allowed_covers_every_state(conn):
    """A state missing from ALLOWED would raise KeyError instead of refusing."""
    assert set(ALLOWED) == set(CaseState)
    for state in NON_TERMINAL:
        assert CaseState.RESOLVED_EXTERNALLY in ALLOWED[state]


# ------------------------------------------------------------- version guard

def test_stale_expected_version_raises_and_leaves_state_unchanged(conn):
    new_case(conn)
    transition(conn, CASE_ID, CaseState.CLASSIFIED, 0, reason="first_writer")
    before = fetch_case(conn, CASE_ID)

    # Second writer still holding version 0 -- the state it decided against.
    with pytest.raises(StaleVersionError):
        transition(conn, CASE_ID, CaseState.ASSIGNED, 0, reason="second_writer")

    after = fetch_case(conn, CASE_ID)
    assert after.state is before.state is CaseState.CLASSIFIED
    assert after.version == before.version == 1


def test_stale_transition_writes_no_transition_row(conn):
    new_case(conn)
    transition(conn, CASE_ID, CaseState.CLASSIFIED, 0)
    with pytest.raises(StaleVersionError):
        transition(conn, CASE_ID, CaseState.ASSIGNED, 0)
    assert count(conn, "case_transitions") == 1


def test_stale_writer_succeeds_after_refetching(conn):
    """The documented recovery: re-fetch, decide again, write with the fresh version."""
    new_case(conn)
    transition(conn, CASE_ID, CaseState.CLASSIFIED, 0)
    with pytest.raises(StaleVersionError):
        transition(conn, CASE_ID, CaseState.ASSIGNED, 0)
    fresh = fetch_case(conn, CASE_ID)
    moved = transition(conn, CASE_ID, CaseState.ASSIGNED, fresh.version)
    assert moved.state is CaseState.ASSIGNED
    assert moved.version == 2


# -------------------------------------------------------- append-only history

def test_every_transition_appends_exactly_one_row(conn):
    new_case(conn)
    case = fetch_case(conn, CASE_ID)
    for expected, step in enumerate(ROUTES[CaseState.ACTION_EXECUTED], start=1):
        case = transition(conn, CASE_ID, step, case.version, reason=f"step_{expected}")
        assert count(conn, "case_transitions") == expected


def test_transition_row_records_both_ends(conn):
    new_case(conn)
    transition(conn, CASE_ID, CaseState.CLASSIFIED, 0,
               reason="root_cause_known", event_id="evt_TESTFIXTURE001")
    (row,) = transitions_for(conn, CASE_ID)
    assert row.from_state is CaseState.OBSERVED_FAILED
    assert row.to_state is CaseState.CLASSIFIED
    assert row.reason == "root_cause_known"
    assert row.event_id == "evt_TESTFIXTURE001"


def test_history_is_ordered_and_complete(conn):
    new_case(conn)
    drive_to(conn, CASE_ID, CaseState.ACTION_EXECUTED)
    assert [t.to_state for t in transitions_for(conn, CASE_ID)] == list(
        ROUTES[CaseState.ACTION_EXECUTED]
    )


# ------------------------------------------------------------ terminal states

@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda s: s.value),
                         ids=lambda s: s.value)
def test_terminal_state_rejects_further_transitions(conn, terminal):
    case_id = f"case_term_{terminal.value}"
    new_case(conn, case_id=case_id)

    if terminal is CaseState.RESOLVED_EXTERNALLY:
        case = drive_to(conn, case_id, CaseState.CLASSIFIED)
    else:
        case = drive_to(conn, case_id, CaseState.ACTION_EXECUTED)
    case = transition(conn, case_id, terminal, case.version, reason="closing")

    before_rows = count(conn, "case_transitions")
    for target in CaseState:
        with pytest.raises(TerminalStateError):
            transition(conn, case_id, target, case.version)

    settled = fetch_case(conn, case_id)
    assert settled.state is terminal
    assert settled.version == case.version
    assert count(conn, "case_transitions") == before_rows


def test_terminal_error_is_an_illegal_transition(conn):
    """Callers that only care 'the move was refused' catch one exception."""
    new_case(conn)
    case = transition(conn, CASE_ID, CaseState.RESOLVED_EXTERNALLY, 0, reason="order.paid")
    with pytest.raises(IllegalTransitionError):
        transition(conn, CASE_ID, CaseState.CLASSIFIED, case.version)
