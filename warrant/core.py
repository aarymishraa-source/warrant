"""
Case lifecycle: one state machine, one append-only transition log.

Three properties this module exists to hold, none of which are obvious:

1. **Payment state is not monotonic.** `order.paid` can arrive at any moment --
   while a case is still being classified, while an intervention sits queued,
   after an action already went out. So `resolved_externally` is reachable from
   every non-terminal state, not just the late ones. A machine that only walked
   forward would have to drop those events, and dropping them is exactly how a
   recovery system ends up claiming credit for a payment it did not cause.

2. **Deliveries race.** Two webhooks for the same case can be processed
   concurrently. `transition()` therefore takes an `expected_version` and
   enforces it in the UPDATE itself (`WHERE case_id=? AND version=?`), using
   rowcount as the authority. Reading the version with a SELECT and then writing
   would lose the race silently: both callers read version 3, both write
   version 4, and one transition disappears with no error raised.

3. **The transition log is append-only.** Nothing here UPDATEs or DELETEs
   `case_transitions`. A transition we regret is corrected by appending another
   one, because Day 10's ledger has to reconstruct what we believed and when,
   not what we wish we had believed.

No decision logic lives here. *Whether* a case should move is Days 6-8; this
module only says whether a move is structurally legal, and records it.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from enum import Enum


class CaseState(str, Enum):
    OBSERVED_FAILED = "observed_failed"
    CLASSIFIED = "classified"
    ASSIGNED = "assigned"
    ACTION_QUEUED = "action_queued"
    ACTION_EXECUTED = "action_executed"
    RESOLVED_BY_ACTION = "resolved_by_action"
    RESOLVED_EXTERNALLY = "resolved_externally"
    EXHAUSTED = "exhausted"


TERMINAL_STATES: frozenset[CaseState] = frozenset({
    CaseState.RESOLVED_BY_ACTION,
    CaseState.RESOLVED_EXTERNALLY,
    CaseState.EXHAUSTED,
})

# The two case types the simulator generates (CALIBRATION.md). Deliberately NOT
# enforced as a closed set: `lending_emi` is a carve-out class the simulator
# never produces but the pre-registered plan still has to handle (EXPERIMENT.md,
# and warrant/assign.py). Rejecting unknown types here would make that carve-out
# unreachable, which is the wrong trade.
SIMULATED_CASE_TYPES = ("one_time_link", "upi_autopay")

ALLOWED: dict[CaseState, frozenset[CaseState]] = {
    CaseState.OBSERVED_FAILED: frozenset({
        CaseState.CLASSIFIED,
        CaseState.RESOLVED_EXTERNALLY,
    }),
    CaseState.CLASSIFIED: frozenset({
        CaseState.ASSIGNED,
        CaseState.RESOLVED_EXTERNALLY,
    }),
    # assigned -> exhausted is the DO_NOTHING path: the controller declined to
    # act, or a policy gate rejected every candidate action, and the 7-day
    # window closed. Those cases stay in their arm's denominator (EXPERIMENT.md).
    CaseState.ASSIGNED: frozenset({
        CaseState.ACTION_QUEUED,
        CaseState.EXHAUSTED,
        CaseState.RESOLVED_EXTERNALLY,
    }),
    CaseState.ACTION_QUEUED: frozenset({
        CaseState.ACTION_EXECUTED,
        CaseState.EXHAUSTED,
        CaseState.RESOLVED_EXTERNALLY,
    }),
    # action_executed -> action_queued is the ONLY cycle in this machine, and it
    # is load-bearing: MAX_ATTEMPTS_PER_CASE is 3, so a case must be able to
    # queue a second and third attempt. The cap itself is enforced by the policy
    # engine (Day 6), not here -- the state machine says what is structurally
    # possible, the policy engine says what is permitted.
    CaseState.ACTION_EXECUTED: frozenset({
        CaseState.ACTION_QUEUED,
        CaseState.RESOLVED_BY_ACTION,
        CaseState.EXHAUSTED,
        CaseState.RESOLVED_EXTERNALLY,
    }),
    CaseState.RESOLVED_BY_ACTION: frozenset(),
    CaseState.RESOLVED_EXTERNALLY: frozenset(),
    CaseState.EXHAUSTED: frozenset(),
}


@dataclass(frozen=True)
class Case:
    case_id: str
    payment_id: str | None
    order_id: str | None
    customer_id: str
    case_type: str
    state: CaseState
    version: int
    ticket_amount_paise: int
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class Transition:
    id: int
    case_id: str
    from_state: CaseState | None
    to_state: CaseState
    reason: str | None
    event_id: str | None
    at: str


class CaseNotFoundError(Exception):
    """Raised when a case_id has no row. Never silently creates one."""


class IllegalTransitionError(Exception):
    """Raised when the requested edge is not in ALLOWED."""


class TerminalStateError(IllegalTransitionError):
    """Raised when a terminal case is asked to move.

    A subclass because it is a more specific reason for the same refusal:
    callers that only care that the move was rejected need not distinguish.
    """


class StaleVersionError(Exception):
    """Raised when expected_version did not match the row at write time.

    The case was mutated between the caller reading it and the caller writing.
    Nothing was changed. The caller must re-fetch and decide again against the
    authoritative state -- retrying with the same expected_version cannot work.
    """


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _row_to_case(row: sqlite3.Row) -> Case:
    return Case(
        case_id=row["case_id"],
        payment_id=row["payment_id"],
        order_id=row["order_id"],
        customer_id=row["customer_id"],
        case_type=row["case_type"],
        state=CaseState(row["state"]),
        version=row["version"],
        ticket_amount_paise=row["ticket_amount_paise"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def is_terminal(state: CaseState | str) -> bool:
    return CaseState(state) in TERMINAL_STATES


def create_case(
    conn: sqlite3.Connection,
    case_id: str,
    customer_id: str,
    case_type: str,
    ticket_amount_paise: int,
    payment_id: str | None = None,
    order_id: str | None = None,
) -> Case:
    """Open a case at `observed_failed`, version 0.

    Creation is not a transition and writes no `case_transitions` row: there is
    no from_state, and `cases.created_at` already records the birth. Keeping the
    log to real edges means a transition count is a transition count.
    """
    now = _now()
    conn.execute(
        """INSERT INTO cases (case_id, payment_id, order_id, customer_id, case_type,
                              state, version, ticket_amount_paise, created_at, updated_at)
           VALUES (?,?,?,?,?,?,0,?,?,?)""",
        (case_id, payment_id, order_id, customer_id, case_type,
         CaseState.OBSERVED_FAILED.value, ticket_amount_paise, now, now),
    )
    conn.commit()
    return fetch_case(conn, case_id)


def fetch_case(conn: sqlite3.Connection, case_id: str) -> Case:
    """Authoritative re-fetch.

    Always read through this before deciding: a `Case` held from earlier in a
    request may already have been superseded by another delivery.
    """
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        raise CaseNotFoundError(case_id)
    return _row_to_case(row)


def transitions_for(conn: sqlite3.Connection, case_id: str) -> tuple[Transition, ...]:
    """The append-only history of one case, oldest first."""
    rows = conn.execute(
        "SELECT * FROM case_transitions WHERE case_id = ? ORDER BY id", (case_id,)
    ).fetchall()
    return tuple(
        Transition(
            id=r["id"],
            case_id=r["case_id"],
            from_state=CaseState(r["from_state"]) if r["from_state"] else None,
            to_state=CaseState(r["to_state"]),
            reason=r["reason"],
            event_id=r["event_id"],
            at=r["at"],
        )
        for r in rows
    )


def transition(
    conn: sqlite3.Connection,
    case_id: str,
    to_state: CaseState | str,
    expected_version: int,
    reason: str | None = None,
    event_id: str | None = None,
) -> Case:
    """Move a case, or change nothing at all.

    Raises `CaseNotFoundError`, `TerminalStateError`, `IllegalTransitionError`
    or `StaleVersionError`. On every failure path the row is left untouched.

    The legality check reads the current state, but that read is advisory only.
    The guarantee comes from the UPDATE's `WHERE case_id=? AND version=?`: if
    anything moved the case between the read and the write, rowcount is 0 and we
    raise `StaleVersionError` rather than overwrite a newer state. This is the
    mechanism NOTES.md means by "version guard prevents state regression".
    """
    target = CaseState(to_state)
    current = fetch_case(conn, case_id)

    if current.state in TERMINAL_STATES:
        raise TerminalStateError(
            f"case {case_id} is terminal in {current.state.value}; "
            f"refused move to {target.value}"
        )

    if target not in ALLOWED[current.state]:
        raise IllegalTransitionError(
            f"{current.state.value} -> {target.value} is not a legal transition"
        )

    now = _now()
    try:
        cur = conn.execute(
            """UPDATE cases SET state = ?, version = version + 1, updated_at = ?
               WHERE case_id = ? AND version = ?""",
            (target.value, now, case_id, expected_version),
        )
        if cur.rowcount != 1:
            conn.rollback()
            actual = fetch_case(conn, case_id)
            raise StaleVersionError(
                f"case {case_id}: expected version {expected_version}, found "
                f"{actual.version} (state {actual.state.value}); nothing written"
            )

        conn.execute(
            """INSERT INTO case_transitions (case_id, from_state, to_state, reason, event_id, at)
               VALUES (?,?,?,?,?,?)""",
            (case_id, current.state.value, target.value, reason, event_id, now),
        )
        conn.commit()
    except StaleVersionError:
        raise
    except Exception:
        # The UPDATE and the log entry are one unit. A case that moved without a
        # transition row would be invisible to the ledger.
        conn.rollback()
        raise

    return fetch_case(conn, case_id)
