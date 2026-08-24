"""
Experimental arm assignment. Deterministic, customer-level, and early.

Why it is hashed rather than randomised
---------------------------------------
`arm = f(sha256(customer_id + ASSIGNMENT_SALT))` is a pure function of the
customer. Drawing a random number per case would put the same customer in
HOLDOUT for one failed payment and WARRANT for the next, which leaks treatment
across that customer's own cases: the reminder they got on Tuesday is still
working on them on Thursday, and the "untreated" observation is not untreated.
Hashing also means assignment needs no state to be reproducible -- rebuilding
the database does not reshuffle the arms.

Why assignment happens FIRST
----------------------------
`assign_case()` must run before any model, heuristic, rule table or LLM reads
the case. Assigning after a model has looked at a case makes arm membership a
function of what the model saw, which is selection on the treatment path and
silently destroys the comparison. Nothing downstream of this module may write
to `assignments`.

Why carve-outs are evaluated before assignment, not after
---------------------------------------------------------
Carve-outs are a property of the case, not of its arm. Evaluating them first
and logging them means the exclusion is visible in the audit trail with its
reason attached. Carved-out cases are never assigned to HOLDOUT -- they always
get treatment -- because not every business can accept a holdout on its largest
accounts or on lending EMIs, and pretending otherwise would be dishonest product
design (EXPERIMENT.md). They remain in the ledger and in their treatment arm's
denominator; they are excluded from the control group, not from measurement.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass

from warrant.config import (
    ARM_WEIGHTS,
    ARMS,
    ASSIGNMENT_SALT,
    HIGH_VALUE_THRESHOLD_PAISE,
)
from warrant.core import Case

HOLDOUT = "HOLDOUT"

# Case types that are never held out. `lending_emi` is not one of the two types
# the simulator generates (CALIBRATION.md); it is the carve-out class the
# pre-registered plan commits to handling, and it is honoured here so that a
# real deployment carrying such cases does not silently hold them out.
CARVED_OUT_CASE_TYPES = ("lending_emi",)

CARVE_HIGH_VALUE = "high_value_ticket"
CARVE_CASE_TYPE = "case_type_lending_emi"

# The arms a carved-out case may receive: everything except the control group,
# reweighted among themselves so the treatment split stays proportional.
TREATMENT_ARMS = tuple(a for a in ARMS if a != HOLDOUT)


@dataclass(frozen=True)
class Assignment:
    case_id: str
    customer_id: str
    arm: str
    carved_out: bool
    carve_reason: str | None
    assigned_at: str


class AlreadyAssignedError(Exception):
    """Raised when a case is re-assigned to a different arm than it already has.

    Re-running assignment for the same case is fine and returns the stored row;
    changing an arm after the fact is not, because the ITT denominator is fixed
    at assignment time.
    """


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _audit(conn: sqlite3.Connection, action: str, case_id: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, event_id, detail_json) VALUES (?,?,?,?,?)",
        (_now(), "assign", action, None, json.dumps({"case_id": case_id, **detail}, sort_keys=True)),
    )


def _unit_interval(customer_id: str) -> float:
    """sha256(customer_id + salt) -> a stable float in [0, 1).

    The first 8 bytes of the digest are read as a big-endian integer and divided
    by 2**64. Using the digest whole (rather than, say, `int(digest, 16) % 3`)
    keeps the mapping independent of the number of arms, so the bucket walk
    below can express 20/40/40 exactly rather than only equal splits.
    """
    digest = hashlib.sha256((customer_id + ASSIGNMENT_SALT).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def _bucket(u: float, weights: dict[str, float]) -> str:
    """Walk cumulative weights in a fixed order and return the arm `u` lands in."""
    total = sum(weights.values())
    cumulative = 0.0
    arm = None
    for arm in weights:
        cumulative += weights[arm] / total
        if u < cumulative:
            return arm
    # Only reachable through floating-point drift at the very top of the range.
    return arm


def arm_for_customer(customer_id: str, *, carved_out: bool = False) -> str:
    """The arm this customer belongs to. Same input, same output, forever.

    A carved-out customer is drawn from the treatment arms only, using the same
    hash, so the carve-out changes *which set* is drawn from and nothing else.
    """
    u = _unit_interval(customer_id)
    if carved_out:
        return _bucket(u, {a: ARM_WEIGHTS[a] for a in TREATMENT_ARMS})
    return _bucket(u, {a: ARM_WEIGHTS[a] for a in ARMS})


def carve_reasons(case: Case) -> tuple[str, ...]:
    """Every carve-out rule this case trips, in a fixed order.

    All rules are evaluated, not just the first: a case can be both a high-value
    ticket and a lending EMI, and the audit trail should say so.
    """
    reasons: list[str] = []
    if case.ticket_amount_paise > HIGH_VALUE_THRESHOLD_PAISE:
        reasons.append(CARVE_HIGH_VALUE)
    if case.case_type in CARVED_OUT_CASE_TYPES:
        reasons.append(CARVE_CASE_TYPE)
    return tuple(reasons)


def get_assignment(conn: sqlite3.Connection, case_id: str) -> Assignment | None:
    row = conn.execute("SELECT * FROM assignments WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    return Assignment(
        case_id=row["case_id"],
        customer_id=row["customer_id"],
        arm=row["arm"],
        carved_out=bool(row["carved_out"]),
        carve_reason=row["carve_reason"],
        assigned_at=row["assigned_at"],
    )


def assign_case(conn: sqlite3.Connection, case: Case) -> Assignment:
    """Assign one case to an arm. Call this BEFORE anything reads the case.

    No model, heuristic, rule lookup or LLM may run against a case that has not
    been assigned: arm membership must not be a function of anything a decision
    component saw. Carve-outs are evaluated and audited first, then the arm is
    drawn -- from the treatment arms only if the case was carved out.

    Idempotent. A second call for the same case returns the stored assignment
    rather than re-drawing, and raises `AlreadyAssignedError` if the stored arm
    disagrees with what would be drawn now (which means the salt or the weights
    moved underneath a live experiment).
    """
    reasons = carve_reasons(case)
    carved = bool(reasons)
    reason_text = ",".join(reasons) if reasons else None

    # Logged before the draw, so the audit trail shows the exclusion decision was
    # made on case properties and not chosen to fit the arm that came out.
    _audit(conn, "carve_out_evaluated", case.case_id, {
        "carved_out": carved,
        "carve_reason": reason_text,
        "case_type": case.case_type,
        "ticket_amount_paise": case.ticket_amount_paise,
    })

    arm = arm_for_customer(case.customer_id, carved_out=carved)
    now = _now()

    cur = conn.execute(
        """INSERT INTO assignments (case_id, customer_id, arm, carved_out, carve_reason, assigned_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(case_id) DO NOTHING""",
        (case.case_id, case.customer_id, arm, int(carved), reason_text, now),
    )

    if cur.rowcount == 1:
        _audit(conn, "arm_assigned", case.case_id, {
            "customer_id": case.customer_id,
            "arm": arm,
            "carved_out": carved,
            "carve_reason": reason_text,
        })
        conn.commit()
        return Assignment(case.case_id, case.customer_id, arm, carved, reason_text, now)

    existing = get_assignment(conn, case.case_id)
    assert existing is not None  # the conflict proves the row is there
    if existing.arm != arm:
        conn.commit()  # keep the carve-out evaluation record
        raise AlreadyAssignedError(
            f"case {case.case_id} is already in {existing.arm}; "
            f"assignment now yields {arm}. Arms are fixed at assignment time."
        )

    _audit(conn, "assignment_reused", case.case_id, {"arm": existing.arm})
    conn.commit()
    return existing
