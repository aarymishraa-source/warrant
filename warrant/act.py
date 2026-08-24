"""
The intent ledger and the actuator. Everything here exists because the network
is not a function call.

**Intent before call.** The intent row is persisted, and committed, before the
external call is made -- never the other way round. If the process dies between
the two, the intent survives and reconciliation can find out what happened. An
actuator that calls first and records afterwards has a window in which money has
moved and nothing in our system knows it.

**A timeout means we do not know whether the action happened. status becomes
UNKNOWN, never FAILED.** This is the whole discipline of the module. FAILED is a
positive claim -- the provider told us it refused and created nothing -- and it
needs evidence. A timeout is the absence of evidence, not evidence of absence:
the request may well have been received, processed, and the payment link
created, with only the response lost. Marking that FAILED and retrying is how a
customer gets two payment links for one failed payment.

**The idempotency key is ours.** It is generated deterministically from
`case_id` + attempt number, so the same attempt always produces the same key
however many times the code runs. Razorpay documents idempotency keys for
Payouts only; for Orders, Payments and Payment Links it is undocumented, so we
do not rely on it. The guarantee is our ledger and its UNIQUE constraint, not
theirs. Do not claim otherwise in the demo.

**Retry only after listing.** `retry()` takes a `Reconciliation`, not an
`Intent`, so a retry is unreachable without having listed the provider's objects
first and found nothing. Retrying blind is how the second payment link gets
created; the type is the discipline.

The external call arrives as an injected callable. This module imports no HTTP
client and opens no socket -- `requests` appears nowhere in it, which is why the
tests can cover the timeout path without a network.

Not in scope here: moving the case through its state machine. `core.transition()`
needs the authoritative version at the moment of writing, and an actuator that
guessed it would race the very deliveries the version guard exists to survive.
The controller that owns both is Day 8.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from warrant.core import Case
from warrant.policy import Decision, Proposal, Verdict

# Razorpay's Payment Links accept a merchant-supplied `reference_id`. We bound
# our key to 40 characters and hash anything longer, rather than discover the
# provider's real limit in production. The exact limit is UNVERIFIED against a
# live account -- poc/payloads/ is still empty -- so the bound is ours and is
# deliberately conservative.
MAX_IDEMPOTENCY_KEY_CHARS = 40
KEY_PREFIX = "wrt"


class IntentStatus(str, Enum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


TERMINAL_STATUSES: frozenset[IntentStatus] = frozenset({
    IntentStatus.EXECUTED,
    IntentStatus.CANCELLED,
    IntentStatus.FAILED,
})

# The statuses `cancel_pending` may close. UNKNOWN is included deliberately: an
# intent we never resolved is exactly the one that must not fire again once the
# customer has already paid.
CANCELLABLE_STATUSES: frozenset[IntentStatus] = frozenset({
    IntentStatus.PENDING,
    IntentStatus.UNKNOWN,
})


@dataclass(frozen=True)
class Intent:
    intent_id: str
    case_id: str
    idempotency_key: str
    action_type: str
    action_cost_paise: int
    status: IntentStatus
    provider_ref: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class ActionRequest:
    """What the injected callable receives. Carries our key, not the provider's.

    The callable's job is to make one HTTP call with these fields and return
    whatever the provider returned. It must not retry internally: a retry inside
    the callable is invisible to the ledger.
    """
    idempotency_key: str
    action_type: str
    case_id: str
    order_id: str | None
    payment_id: str | None
    customer_id: str
    amount_paise: int
    channel: str
    timing: str


@dataclass(frozen=True)
class Reconciliation:
    """The result of listing the provider's objects for one order.

    `safe_to_retry` is True only when the listing came back and our key was not
    in it. It is the only thing that unlocks `retry()`.
    """
    intent: Intent
    found: bool
    provider_ref: str | None
    safe_to_retry: bool
    reason: str


class ProviderRejected(Exception):
    """Raised BY the injected callable to say the provider definitively refused.

    This is the only signal that produces FAILED. Raise it only when the
    provider's response says nothing was created -- a validation error, a
    rejected payload. If you are unsure, do not raise it: raise anything else, or
    let the timeout propagate, and the intent becomes UNKNOWN.
    """


class RefusedVerdictError(Exception):
    """Raised when the actuator is handed a verdict that is not EXECUTE.

    The policy engine is the authority (warrant/policy.py). This is the second
    lock on the same door: an actuator that could act on a DO_NOTHING verdict
    would make the gates advisory.
    """


class IntentNotRetryableError(Exception):
    """Raised when a retry is attempted without a reconciliation that permits it."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _audit(conn: sqlite3.Connection, action: str, case_id: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, event_id, detail_json) VALUES (?,?,?,?,?)",
        (_now(), "act", action, None, json.dumps({"case_id": case_id, **detail}, sort_keys=True)),
    )


def _field(obj: object, name: str):
    """Read one field from a provider object, whether it is a dict or a model.

    Provider SDKs return dicts; test fixtures and typed clients return objects.
    Neither should dictate the shape of the ledger.
    """
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def idempotency_key(case_id: str, attempt: int) -> str:
    """Deterministic key for one attempt on one case. Ours, not the provider's.

    The same (case, attempt) always yields the same key, on any machine, after
    any restart -- which is what makes a retry safe and a duplicate detectable.
    Readable while it fits, hashed when it would exceed the bound, because a key
    a human can trace in a provider dashboard is worth keeping when we can.
    """
    readable = f"{KEY_PREFIX}_{case_id}_a{attempt}"
    if len(readable) <= MAX_IDEMPOTENCY_KEY_CHARS:
        return readable
    digest = hashlib.sha256(readable.encode()).hexdigest()
    return f"{KEY_PREFIX}_{digest}"[:MAX_IDEMPOTENCY_KEY_CHARS]


def _intent_id(case_id: str, attempt: int) -> str:
    return f"int_{hashlib.sha256(f'{case_id}:{attempt}'.encode()).hexdigest()[:24]}"


def _row_to_intent(row: sqlite3.Row) -> Intent:
    return Intent(
        intent_id=row["intent_id"],
        case_id=row["case_id"],
        idempotency_key=row["idempotency_key"],
        action_type=row["action_type"],
        action_cost_paise=row["action_cost_paise"],
        status=IntentStatus(row["status"]),
        provider_ref=row["provider_ref"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def fetch_intent(conn: sqlite3.Connection, intent_id: str) -> Intent | None:
    row = conn.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
    return None if row is None else _row_to_intent(row)


def intent_by_key(conn: sqlite3.Connection, key: str) -> Intent | None:
    row = conn.execute("SELECT * FROM intents WHERE idempotency_key = ?", (key,)).fetchone()
    return None if row is None else _row_to_intent(row)


def intents_for_case(conn: sqlite3.Connection, case_id: str) -> tuple[Intent, ...]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE case_id = ? ORDER BY created_at, intent_id", (case_id,)
    ).fetchall()
    return tuple(_row_to_intent(r) for r in rows)


def next_attempt(conn: sqlite3.Connection, case_id: str) -> int:
    """Attempt number for the next NEW intent on this case, 1-based.

    Counts intents, not outcomes: a timed-out attempt consumed an attempt even
    though we never learned what it did.
    """
    return conn.execute(
        "SELECT COUNT(*) AS n FROM intents WHERE case_id = ?", (case_id,)
    ).fetchone()["n"] + 1


def _settle(conn: sqlite3.Connection, intent: Intent, status: IntentStatus,
            provider_ref: str | None, audit_action: str, detail: dict) -> Intent:
    """Move one intent to a new status and audit it. Every status change goes
    through here, so no state change can happen without a record."""
    now = _now()
    resolved_at = None if status is IntentStatus.PENDING else now
    conn.execute(
        "UPDATE intents SET status = ?, provider_ref = ?, resolved_at = ? WHERE intent_id = ?",
        (status.value, provider_ref, resolved_at, intent.intent_id),
    )
    _audit(conn, audit_action, intent.case_id, {
        "intent_id": intent.intent_id,
        "idempotency_key": intent.idempotency_key,
        "from_status": intent.status.value,
        "to_status": status.value,
        "provider_ref": provider_ref,
        **detail,
    })
    conn.commit()
    settled = fetch_intent(conn, intent.intent_id)
    assert settled is not None
    return settled


def execute(
    conn: sqlite3.Connection,
    case: Case,
    proposal: Proposal,
    verdict: Verdict,
    caller,
    attempt: int | None = None,
) -> Intent:
    """Record an intent, then make the call. In that order, always.

    `caller` is invoked with one `ActionRequest` and returns whatever the
    provider returned; it may raise `ProviderRejected` to report a definitive
    refusal, or `TimeoutError` -- or anything else -- to report that we do not
    know. It is never called at all if an intent for this key already exists.

    Returns the intent in its settled status. Raises `RefusedVerdictError` if the
    verdict is not EXECUTE; otherwise it does not raise, because a call whose
    outcome is unknown is a result, not an error.
    """
    if verdict.decision is not Decision.EXECUTE:
        raise RefusedVerdictError(
            f"case {case.case_id}: verdict is {verdict.decision.value} "
            f"({verdict.rule_id}); the actuator does not act on refusals"
        )
    if verdict.action_cost_paise is None:
        raise RefusedVerdictError(
            f"case {case.case_id}: verdict carries no priced cost"
        )

    n = next_attempt(conn, case.case_id) if attempt is None else attempt
    key = idempotency_key(case.case_id, n)
    intent_id = _intent_id(case.case_id, n)
    now = _now()

    # The UNIQUE constraint does the deduplication, not a SELECT-then-INSERT:
    # two concurrent deliveries would both read "no intent" and both call out.
    # A bare DO NOTHING covers both unique constraints -- the primary key and the
    # idempotency key derive from the same (case, attempt) pair.
    cur = conn.execute(
        """INSERT INTO intents (intent_id, case_id, idempotency_key, action_type,
                                action_cost_paise, status, provider_ref, created_at, resolved_at)
           VALUES (?,?,?,?,?,?,NULL,?,NULL)
           ON CONFLICT DO NOTHING""",
        (intent_id, case.case_id, key, proposal.action,
         verdict.action_cost_paise, IntentStatus.PENDING.value, now),
    )

    if cur.rowcount != 1:
        existing = intent_by_key(conn, key)
        assert existing is not None  # the conflict proves the row is there
        _audit(conn, "intent_reused", case.case_id, {
            "intent_id": existing.intent_id,
            "idempotency_key": key,
            "status": existing.status.value,
            "note": "duplicate execution suppressed; the provider was not called",
        })
        conn.commit()
        return existing

    _audit(conn, "intent_created", case.case_id, {
        "intent_id": intent_id,
        "idempotency_key": key,
        "action_type": proposal.action,
        "action_cost_paise": verdict.action_cost_paise,
        "attempt": n,
        "status": IntentStatus.PENDING.value,
    })
    # Committed BEFORE the call. Everything below this line can die without
    # losing the fact that we were about to act.
    conn.commit()

    intent = fetch_intent(conn, intent_id)
    assert intent is not None
    request = ActionRequest(
        idempotency_key=key,
        action_type=proposal.action,
        case_id=case.case_id,
        order_id=case.order_id,
        payment_id=case.payment_id,
        customer_id=case.customer_id,
        amount_paise=case.ticket_amount_paise,
        channel=proposal.channel,
        timing=proposal.timing,
    )

    try:
        response = caller(request)
    except ProviderRejected as exc:
        # The only path to FAILED: the provider told us it created nothing.
        return _settle(conn, intent, IntentStatus.FAILED, None, "intent_failed", {
            "reason": f"provider rejected the request: {exc}",
        })
    except TimeoutError as exc:
        # A timeout means we do not know whether the action happened.
        # status becomes UNKNOWN, never FAILED.
        return _settle(conn, intent, IntentStatus.UNKNOWN, None, "intent_unknown", {
            "reason": f"timeout: {exc}",
            "note": "not a failure; reconcile by listing before any retry",
        })
    except Exception as exc:  # noqa: BLE001 -- deliberate: unclassified is unknown
        # Anything we cannot classify is uncertainty, not failure. A connection
        # reset after the request was written to the socket looks exactly like a
        # request that was never sent.
        return _settle(conn, intent, IntentStatus.UNKNOWN, None, "intent_unknown", {
            "reason": f"{type(exc).__name__}: {exc}",
            "note": "unclassified transport error; reconcile by listing before any retry",
        })

    provider_ref = _field(response, "id")
    if provider_ref is None:
        # The call returned, but not something we can point at. We still do not
        # know what exists at the provider, so we still do not claim.
        return _settle(conn, intent, IntentStatus.UNKNOWN, None, "intent_unknown", {
            "reason": "provider response carried no id",
            "note": "reconcile by listing before any retry",
        })

    return _settle(conn, intent, IntentStatus.EXECUTED, str(provider_ref), "intent_executed", {
        "reason": "provider returned an object",
    })


def _matches_key(obj: object, key: str) -> bool:
    """Is this provider object the one our key created?

    Checked against the fields a merchant can actually set: `reference_id` on a
    Payment Link, and `notes`, which Razorpay carries through on most entities.
    Matching on amount or on a timestamp would be a guess, and a guess here means
    either a duplicate charge or a lost one.
    """
    if _field(obj, "reference_id") == key:
        return True
    notes = _field(obj, "notes")
    if isinstance(notes, Mapping) and notes.get("idempotency_key") == key:
        return True
    return False


def reconcile(conn: sqlite3.Connection, intent: Intent, lister) -> Reconciliation:
    """Ask the provider what exists, by listing -- never by creating.

    `lister` is called with the order id and returns the provider objects for
    that order. If one of them carries our idempotency key, the action DID
    happen: the intent becomes EXECUTED with the provider's reference, and no
    second object is ever created.

    If the listing comes back without our key, the intent stays UNKNOWN and
    becomes retryable. It does not become FAILED. A listing that shows an object
    is strong evidence; a listing that does not is weak -- provider listings lag,
    and an object created a second ago may not appear yet. Retrying with the same
    key is safe precisely because it is the same key.

    A CANCELLED intent is never resurrected here. If the customer has already
    paid, what the provider is still holding is a link we no longer want; the
    reference is recorded as evidence and the status left alone.
    """
    current = fetch_intent(conn, intent.intent_id) or intent
    case_row = conn.execute(
        "SELECT order_id FROM cases WHERE case_id = ?", (current.case_id,)
    ).fetchone()
    order_id = case_row["order_id"] if case_row else None

    objects = list(lister(order_id) or ())
    match = next((o for o in objects if _matches_key(o, current.idempotency_key)), None)
    provider_ref = None if match is None else str(_field(match, "id"))

    if current.status is IntentStatus.CANCELLED:
        # The race: order.paid landed while this intent was in flight. Recording
        # what exists at the provider is useful; reopening a cancelled intent
        # would send a payment link to a customer who has already paid.
        _audit(conn, "reconciled_after_cancellation", current.case_id, {
            "intent_id": current.intent_id,
            "idempotency_key": current.idempotency_key,
            "found_at_provider": match is not None,
            "provider_ref": provider_ref,
            "note": "intent stays CANCELLED; a cancelled intent is never resurrected",
        })
        conn.commit()
        return Reconciliation(
            intent=current, found=match is not None, provider_ref=provider_ref,
            safe_to_retry=False,
            reason="intent was cancelled; not resurrected",
        )

    if current.status in TERMINAL_STATUSES:
        _audit(conn, "reconcile_noop", current.case_id, {
            "intent_id": current.intent_id,
            "idempotency_key": current.idempotency_key,
            "status": current.status.value,
            "note": "already settled; nothing to reconcile",
        })
        conn.commit()
        return Reconciliation(
            intent=current, found=match is not None, provider_ref=current.provider_ref,
            safe_to_retry=False,
            reason=f"intent already settled as {current.status.value}",
        )

    if match is not None:
        settled = _settle(conn, current, IntentStatus.EXECUTED, provider_ref,
                          "intent_reconciled_executed", {
                              "reason": "provider listing carried our idempotency key",
                              "objects_listed": len(objects),
                          })
        return Reconciliation(
            intent=settled, found=True, provider_ref=provider_ref, safe_to_retry=False,
            reason="action did happen; found at the provider",
        )

    _audit(conn, "reconciled_not_found", current.case_id, {
        "intent_id": current.intent_id,
        "idempotency_key": current.idempotency_key,
        "objects_listed": len(objects),
        "status": current.status.value,
        "note": "no object carries our key; retry is safe with the SAME key",
    })
    conn.commit()
    return Reconciliation(
        intent=current, found=False, provider_ref=None, safe_to_retry=True,
        reason="no object at the provider carries our key",
    )


def retry(conn: sqlite3.Connection, reconciliation: Reconciliation, caller) -> Intent:
    """Re-fire an intent the provider does not have, reusing the SAME key.

    Takes a `Reconciliation` rather than an `Intent` on purpose: you cannot reach
    this function without having listed first. No new intent row is created and
    no new key is generated -- a second key would be a second payment link.
    """
    if not reconciliation.safe_to_retry:
        raise IntentNotRetryableError(
            f"intent {reconciliation.intent.intent_id}: {reconciliation.reason}"
        )

    intent = fetch_intent(conn, reconciliation.intent.intent_id)
    if intent is None:
        raise IntentNotRetryableError(f"intent {reconciliation.intent.intent_id} is gone")
    if intent.status in TERMINAL_STATUSES:
        # Settled between the listing and here -- an order.paid cancellation, for
        # instance. The reconciliation is stale and the retry is refused.
        raise IntentNotRetryableError(
            f"intent {intent.intent_id} settled as {intent.status.value} since reconciliation"
        )

    case_row = conn.execute(
        "SELECT * FROM cases WHERE case_id = ?", (intent.case_id,)
    ).fetchone()
    request = ActionRequest(
        idempotency_key=intent.idempotency_key,  # the same key. always.
        action_type=intent.action_type,
        case_id=intent.case_id,
        order_id=case_row["order_id"] if case_row else None,
        payment_id=case_row["payment_id"] if case_row else None,
        customer_id=case_row["customer_id"] if case_row else "",
        amount_paise=case_row["ticket_amount_paise"] if case_row else 0,
        channel="",
        timing="retry",
    )

    _audit(conn, "intent_retried", intent.case_id, {
        "intent_id": intent.intent_id,
        "idempotency_key": intent.idempotency_key,
        "note": "same key reused; no second intent created",
    })
    conn.commit()

    try:
        response = caller(request)
    except ProviderRejected as exc:
        return _settle(conn, intent, IntentStatus.FAILED, None, "intent_failed", {
            "reason": f"provider rejected the retry: {exc}",
        })
    except Exception as exc:  # noqa: BLE001 -- timeouts included: unknown, not failed
        return _settle(conn, intent, IntentStatus.UNKNOWN, None, "intent_unknown", {
            "reason": f"{type(exc).__name__}: {exc}",
            "note": "still unknown after retry; reconcile again before retrying",
        })

    provider_ref = _field(response, "id")
    if provider_ref is None:
        return _settle(conn, intent, IntentStatus.UNKNOWN, None, "intent_unknown", {
            "reason": "provider response carried no id",
        })
    return _settle(conn, intent, IntentStatus.EXECUTED, str(provider_ref), "intent_executed", {
        "reason": "retry with the original key returned an object",
    })


def cancel_pending(conn: sqlite3.Connection, order_id: str, reason: str) -> tuple[Intent, ...]:
    """Cancel every PENDING or UNKNOWN intent for an order. Called on order.paid.

    UNKNOWN is cancelled too, and that is the point: an intent whose outcome we
    never learned is exactly the one that would otherwise be retried into a
    customer who has already paid.

    This cancels OUR intent, not the provider's object. A payment link already
    created at the provider stays alive there; closing it is a separate call this
    module does not make. Say so plainly rather than implying we reached in and
    revoked something.
    """
    rows = conn.execute(
        """SELECT i.* FROM intents i
             JOIN cases c ON c.case_id = i.case_id
            WHERE c.order_id = ? AND i.status IN (?, ?)
            ORDER BY i.created_at, i.intent_id""",
        (order_id, IntentStatus.PENDING.value, IntentStatus.UNKNOWN.value),
    ).fetchall()

    cancelled = []
    for row in rows:
        intent = _row_to_intent(row)
        cancelled.append(_settle(
            conn, intent, IntentStatus.CANCELLED, intent.provider_ref,
            "intent_cancelled", {
                "reason": reason,
                "order_id": order_id,
                "note": "our intent is cancelled; any provider-side object is not",
            },
        ))

    if not cancelled:
        _audit(conn, "cancel_pending_noop", "", {
            "order_id": order_id,
            "reason": reason,
            "note": "no pending or unknown intents for this order",
        })
        conn.commit()

    return tuple(cancelled)
