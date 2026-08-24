"""
Day 7 actuator tests. No network: every external call is an injected callable,
which is the point of injecting it.

The properties here are the ones that cost real money when they are wrong. An
intent recorded after the call instead of before loses a payment link on a
crash. A timeout read as a failure sends a second one. A cancelled intent that
a late reconciliation resurrects sends one to a customer who has already paid.
"""
from __future__ import annotations

import json

import pytest

from warrant import db
from warrant.act import (
    IntentNotRetryableError,
    IntentStatus,
    ProviderRejected,
    RefusedVerdictError,
    cancel_pending,
    execute,
    fetch_intent,
    idempotency_key,
    intent_by_key,
    intents_for_case,
    next_attempt,
    reconcile,
    retry,
)
from warrant.core import create_case, fetch_case
from warrant.policy import Decision, Proposal, Verdict

CASE_ID = "case_TESTFIXTURE001"
ORDER_ID = "order_TESTFIXTURE001"
CUSTOMER = "cust_TESTFIXTURE001"
LINK_COST_PAISE = 5_500

PROPOSAL = Proposal(
    action="SEND_PAYMENT_LINK",
    timing="immediate",
    channel="sms",
    rationale="card declined at authentication; a fresh link usually clears it",
    confidence=0.90,
)

EXECUTE_VERDICT = Verdict(
    decision=Decision.EXECUTE,
    rule_id="all_gates_passed",
    reason="expected incremental value Rs 68 clears action cost Rs 55",
    expected_value_paise=6_800,
    action_cost_paise=LINK_COST_PAISE,
    evaluated_at="2026-08-24T00:00:00+00:00",
)

REFUSED_VERDICT = Verdict(
    decision=Decision.DO_NOTHING,
    rule_id="ev_threshold",
    reason="expected incremental value Rs 34 does not clear action cost Rs 55",
    expected_value_paise=3_400,
    action_cost_paise=LINK_COST_PAISE,
    evaluated_at="2026-08-24T00:00:00+00:00",
)


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_case(conn, case_id: str = CASE_ID, order_id: str | None = ORDER_ID,
              customer_id: str = CUSTOMER):
    return create_case(
        conn,
        case_id=case_id,
        customer_id=customer_id,
        case_type="one_time_link",
        ticket_amount_paise=34_000,
        payment_id="pay_TESTFIXTURE001",
        order_id=order_id,
    )


class RecordingCaller:
    """Stands in for the provider. Records every request it is handed."""

    def __init__(self, response=None):
        self.requests = []
        self.response = response if response is not None else {"id": "plink_TESTFIXTURE001"}

    def __call__(self, request):
        self.requests.append(request)
        return self.response

    @property
    def calls(self) -> int:
        return len(self.requests)


class TimeoutCaller(RecordingCaller):
    def __call__(self, request):
        self.requests.append(request)
        raise TimeoutError("read timed out after 30s")


class RejectingCaller(RecordingCaller):
    def __call__(self, request):
        self.requests.append(request)
        raise ProviderRejected("amount must be at least 100 paise")


def link_object(key: str, object_id: str = "plink_TESTFIXTURE001") -> dict:
    """A provider object shaped like a Razorpay Payment Link, carrying our key
    in the field a merchant actually controls."""
    return {"id": object_id, "reference_id": key, "status": "created"}


def lister_returning(*objects):
    def lister(order_id):
        return list(objects)
    return lister


def act_audit(conn, action: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT action, detail_json FROM audit_log WHERE actor = 'act' ORDER BY id"
    ).fetchall()
    return [dict(json.loads(r["detail_json"]), action=r["action"])
            for r in rows if action is None or r["action"] == action]


# ------------------------------------------------------------ intent before call

def test_intent_row_exists_before_the_external_call(conn):
    """The callable asserts its own precondition: by the time the provider is
    called, the intent is already in the ledger and already PENDING."""
    seen = {}

    def caller(request):
        row = intent_by_key(conn, request.idempotency_key)
        assert row is not None, "provider was called before the intent was recorded"
        assert row.status is IntentStatus.PENDING
        seen["status_at_call_time"] = row.status
        return {"id": "plink_TESTFIXTURE001"}

    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller)
    assert seen["status_at_call_time"] is IntentStatus.PENDING
    assert intent.status is IntentStatus.EXECUTED


def test_intent_is_committed_before_the_call_not_merely_written(tmp_path):
    """A row written inside an open transaction would vanish if the process died
    mid-call. This reads the ledger from a SECOND connection while the first is
    still inside the call -- if the insert were uncommitted, it would not be
    visible there."""
    path = str(tmp_path / "warrant_test.db")
    first = db.connect(path)
    db.init_db(first)
    case = make_case(first)
    observed = {}

    def caller(request):
        onlooker = db.connect(path)
        try:
            row = onlooker.execute(
                "SELECT status FROM intents WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            observed["visible"] = row is not None
            observed["status"] = None if row is None else row["status"]
        finally:
            onlooker.close()
        return {"id": "plink_TESTFIXTURE001"}

    execute(first, case, PROPOSAL, EXECUTE_VERDICT, caller)
    first.close()
    assert observed["visible"] is True, "intent was not committed before the call"
    assert observed["status"] == "PENDING"


def test_provider_receives_our_idempotency_key(conn):
    case = make_case(conn)
    caller = RecordingCaller()
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller)
    assert caller.requests[0].idempotency_key == intent.idempotency_key
    assert intent.idempotency_key == idempotency_key(CASE_ID, 1)
    assert caller.requests[0].order_id == ORDER_ID


def test_idempotency_key_is_deterministic_and_bounded():
    assert idempotency_key(CASE_ID, 1) == idempotency_key(CASE_ID, 1)
    assert idempotency_key(CASE_ID, 1) != idempotency_key(CASE_ID, 2)
    long_case = "case_" + "x" * 200
    assert len(idempotency_key(long_case, 1)) <= 40
    assert idempotency_key(long_case, 1) == idempotency_key(long_case, 1)


def test_actuator_refuses_a_verdict_that_is_not_execute(conn):
    """The gates would be advisory if the actuator could act on a refusal."""
    case = make_case(conn)
    caller = RecordingCaller()
    with pytest.raises(RefusedVerdictError):
        execute(conn, case, PROPOSAL, REFUSED_VERDICT, caller)
    assert caller.calls == 0
    assert intents_for_case(conn, CASE_ID) == ()


# --------------------------------------------------------- timeout is not failure

def test_timeout_becomes_unknown_never_failed(conn):
    case = make_case(conn)
    caller = TimeoutCaller()
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller)
    assert intent.status is IntentStatus.UNKNOWN
    assert intent.status is not IntentStatus.FAILED
    assert intent.provider_ref is None
    assert intent.resolved_at is not None


def test_unclassified_transport_error_is_also_unknown(conn):
    """A connection reset after the bytes went out looks exactly like a request
    that never left. Absence of evidence is not evidence of absence."""
    def caller(request):
        raise ConnectionResetError("connection reset by peer")

    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller)
    assert intent.status is IntentStatus.UNKNOWN


def test_unreadable_response_is_unknown(conn):
    """The call returned, but with nothing we can point at. We still do not know
    what exists at the provider, so we still do not claim."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, RecordingCaller(response={}))
    assert intent.status is IntentStatus.UNKNOWN


def test_only_a_definitive_rejection_becomes_failed(conn):
    """FAILED is a positive claim and needs the provider to have made it."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, RejectingCaller())
    assert intent.status is IntentStatus.FAILED
    assert intent.provider_ref is None


def test_timeout_is_audited_as_unknown(conn):
    case = make_case(conn)
    execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    rows = act_audit(conn, "intent_unknown")
    assert len(rows) == 1
    assert rows[0]["to_status"] == "UNKNOWN"
    assert "timeout" in rows[0]["reason"]


# ------------------------------------------------------------------ reconcile

def test_reconcile_finds_the_object_and_settles_executed_without_calling_again(conn):
    case = make_case(conn)
    timed_out = TimeoutCaller()
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, timed_out)
    assert intent.status is IntentStatus.UNKNOWN

    # The link existed all along; only the response was lost.
    result = reconcile(conn, intent, lister_returning(link_object(intent.idempotency_key)))

    assert result.found is True
    assert result.safe_to_retry is False
    assert result.intent.status is IntentStatus.EXECUTED
    assert result.intent.provider_ref == "plink_TESTFIXTURE001"
    assert timed_out.calls == 1, "reconciliation must list, never create"
    assert len(intents_for_case(conn, CASE_ID)) == 1


def test_reconcile_ignores_objects_that_are_not_ours(conn):
    """Another link on the same order is not our attempt. Matching on anything
    looser than our key is how you claim someone else's object."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    stranger = link_object("wrt_some_other_case_a1", object_id="plink_SOMEONE_ELSE")

    result = reconcile(conn, intent, lister_returning(stranger))

    assert result.found is False
    assert result.safe_to_retry is True
    assert fetch_intent(conn, intent.intent_id).status is IntentStatus.UNKNOWN


def test_reconcile_matches_on_notes_when_reference_id_is_absent(conn):
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    via_notes = {"id": "plink_VIA_NOTES",
                 "notes": {"idempotency_key": intent.idempotency_key}}

    result = reconcile(conn, intent, lister_returning(via_notes))

    assert result.found is True
    assert result.intent.provider_ref == "plink_VIA_NOTES"


def test_reconcile_finding_nothing_leaves_unknown_not_failed(conn):
    """A listing that shows an object is strong evidence; one that does not is
    weak. Provider listings lag, so this stays UNKNOWN."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    result = reconcile(conn, intent, lister_returning())
    assert result.safe_to_retry is True
    assert result.intent.status is IntentStatus.UNKNOWN
    assert result.intent.status is not IntentStatus.FAILED


# ---------------------------------------------------------------------- retry

def test_retry_after_empty_listing_reuses_the_same_key(conn):
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    original_key = intent.idempotency_key

    result = reconcile(conn, intent, lister_returning())
    second_caller = RecordingCaller(response={"id": "plink_ON_RETRY"})
    retried = retry(conn, result, second_caller)

    assert second_caller.requests[0].idempotency_key == original_key
    assert retried.idempotency_key == original_key
    assert retried.status is IntentStatus.EXECUTED
    assert retried.provider_ref == "plink_ON_RETRY"
    assert len(intents_for_case(conn, CASE_ID)) == 1, "a retry is not a new intent"


def test_retry_is_unreachable_without_a_reconciliation_that_permits_it(conn):
    """The type is the discipline: `retry` takes a Reconciliation, and one that
    found the object refuses to unlock a second call."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    found = reconcile(conn, intent, lister_returning(link_object(intent.idempotency_key)))
    caller = RecordingCaller()
    with pytest.raises(IntentNotRetryableError):
        retry(conn, found, caller)
    assert caller.calls == 0


def test_retry_that_times_out_again_stays_unknown(conn):
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    result = reconcile(conn, intent, lister_returning())
    retried = retry(conn, result, TimeoutCaller())
    assert retried.status is IntentStatus.UNKNOWN
    assert len(intents_for_case(conn, CASE_ID)) == 1


# ------------------------------------------------------------------ duplicates

def test_duplicate_execute_with_the_same_key_makes_one_intent_and_one_call(conn):
    case = make_case(conn)
    caller = RecordingCaller()

    first = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller, attempt=1)
    second = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller, attempt=1)

    assert caller.calls == 1, "the provider was called twice for one attempt"
    assert len(intents_for_case(conn, CASE_ID)) == 1
    assert second.intent_id == first.intent_id
    assert second.idempotency_key == first.idempotency_key
    assert act_audit(conn, "intent_reused")


def test_a_genuine_second_attempt_gets_its_own_key(conn):
    """Distinct from a duplicate delivery: attempt 2 is a new intent, by design."""
    case = make_case(conn)
    caller = RecordingCaller()
    first = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller)
    assert next_attempt(conn, CASE_ID) == 2
    second = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, caller)

    assert first.idempotency_key != second.idempotency_key
    assert caller.calls == 2
    assert len(intents_for_case(conn, CASE_ID)) == 2


# ------------------------------------------------------------- cancel_pending

def test_cancel_pending_cancels_pending_and_unknown_and_leaves_executed_alone(conn):
    make_case(conn, case_id="case_pending", order_id=ORDER_ID, customer_id=CUSTOMER)
    make_case(conn, case_id="case_unknown", order_id=ORDER_ID, customer_id=CUSTOMER)
    make_case(conn, case_id="case_executed", order_id=ORDER_ID, customer_id=CUSTOMER)

    # PENDING: the call never returned at all, so the intent was never settled.
    conn.execute(
        """INSERT INTO intents (intent_id, case_id, idempotency_key, action_type,
                                action_cost_paise, status, provider_ref, created_at, resolved_at)
           VALUES ('int_pending','case_pending','wrt_case_pending_a1','SEND_PAYMENT_LINK',
                   ?, 'PENDING', NULL, '2026-08-24T00:00:00+00:00', NULL)""",
        (LINK_COST_PAISE,),
    )
    conn.commit()

    execute(conn, fetch_case(conn, "case_unknown"), PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    executed = execute(conn, fetch_case(conn, "case_executed"), PROPOSAL, EXECUTE_VERDICT,
                       RecordingCaller())

    cancelled = cancel_pending(conn, ORDER_ID, reason="order.paid")

    assert {i.status for i in cancelled} == {IntentStatus.CANCELLED}
    assert {i.case_id for i in cancelled} == {"case_pending", "case_unknown"}
    assert fetch_intent(conn, executed.intent_id).status is IntentStatus.EXECUTED
    assert all(i.resolved_at is not None for i in cancelled)


def test_cancel_pending_ignores_other_orders(conn):
    make_case(conn, case_id="case_mine", order_id=ORDER_ID)
    make_case(conn, case_id="case_theirs", order_id="order_SOMEONE_ELSE",
              customer_id="cust_other")
    execute(conn, fetch_case(conn, "case_mine"), PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    other = execute(conn, fetch_case(conn, "case_theirs"), PROPOSAL, EXECUTE_VERDICT,
                    TimeoutCaller())

    cancelled = cancel_pending(conn, ORDER_ID, reason="order.paid")

    assert [i.case_id for i in cancelled] == ["case_mine"]
    assert fetch_intent(conn, other.intent_id).status is IntentStatus.UNKNOWN


def test_cancel_pending_is_audited_per_intent(conn):
    case = make_case(conn)
    execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    cancel_pending(conn, ORDER_ID, reason="order.paid")
    rows = act_audit(conn, "intent_cancelled")
    assert len(rows) == 1
    assert rows[0]["reason"] == "order.paid"
    assert rows[0]["to_status"] == "CANCELLED"


def test_cancel_pending_with_nothing_to_cancel_is_recorded(conn):
    make_case(conn)
    assert cancel_pending(conn, ORDER_ID, reason="order.paid") == ()
    assert act_audit(conn, "cancel_pending_noop")


# ------------------------------------------------------------------- the race

def test_late_reconcile_does_not_resurrect_a_cancelled_intent(conn):
    """The full race, in order: an intent is in flight, order.paid arrives and
    cancels it, and only then does the provider listing come back showing the
    link was created. Resurrecting it here sends a payment link to a customer
    who has already paid."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    assert intent.status is IntentStatus.UNKNOWN

    cancelled = cancel_pending(conn, ORDER_ID, reason="order.paid")
    assert cancelled[0].status is IntentStatus.CANCELLED

    # The listing arrives late, and it DOES contain our object.
    result = reconcile(conn, cancelled[0],
                       lister_returning(link_object(intent.idempotency_key)))

    assert result.found is True
    assert result.safe_to_retry is False
    assert result.intent.status is IntentStatus.CANCELLED
    assert fetch_intent(conn, intent.intent_id).status is IntentStatus.CANCELLED
    assert act_audit(conn, "reconciled_after_cancellation")


def test_retry_refuses_an_intent_cancelled_since_reconciliation(conn):
    """The narrower race: the listing said retry was safe, then order.paid landed
    before the retry went out."""
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    result = reconcile(conn, intent, lister_returning())
    assert result.safe_to_retry is True

    cancel_pending(conn, ORDER_ID, reason="order.paid")

    caller = RecordingCaller()
    with pytest.raises(IntentNotRetryableError):
        retry(conn, result, caller)
    assert caller.calls == 0
    assert fetch_intent(conn, intent.intent_id).status is IntentStatus.CANCELLED


# --------------------------------------------------------------------- audit

def test_every_status_change_is_audited(conn):
    case = make_case(conn)
    intent = execute(conn, case, PROPOSAL, EXECUTE_VERDICT, TimeoutCaller())
    reconcile(conn, intent, lister_returning())
    cancel_pending(conn, ORDER_ID, reason="order.paid")

    actions = [r["action"] for r in act_audit(conn)]
    assert actions == [
        "intent_created",
        "intent_unknown",
        "reconciled_not_found",
        "intent_cancelled",
    ]
