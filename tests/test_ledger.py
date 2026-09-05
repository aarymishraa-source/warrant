"""Tests for warrant.ledger."""

from datetime import datetime, timedelta, timezone

from warrant import db
from warrant.act import IntentStatus
from warrant.ledger import (
    get_intents_for_case,
    get_pending_intents,
    resolve,
    stats,
)


def _fresh_db():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


def _insert_case(conn, case_id, customer_id="cust_001", order_id="order_001",
                 case_type="one_time_link", ticket_amount_paise=25000):
    conn.execute(
        """INSERT INTO cases
           (case_id, customer_id, case_type, ticket_amount_paise,
            state, payment_id, order_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (case_id, customer_id, case_type, ticket_amount_paise,
         "action_queued", f"pay_{case_id}", order_id),
    )


def _insert_intent(conn, intent_id, case_id, status, action_type="SEND_PAYMENT_LINK",
                   provider_ref=None, created_at=None):
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO intents
           (intent_id, case_id, idempotency_key, action_type,
            action_cost_paise, status, provider_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (intent_id, case_id, f"key_{intent_id}", action_type,
         5500, status, provider_ref, created_at),
    )


class TestStats:
    def test_empty_stats(self):
        conn = _fresh_db()
        s = stats(conn)
        assert s["total"] == 0
        assert s["executed"] == 0
        assert s["unknown"] == 0
        assert s["pending"] == 0

    def test_counts_by_status(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        _insert_intent(conn, "i001", "case_001", "EXECUTED")
        _insert_intent(conn, "i002", "case_001", "FAILED")
        _insert_intent(conn, "i003", "case_001", "UNKNOWN")
        _insert_intent(conn, "i004", "case_001", "PENDING")
        _insert_intent(conn, "i005", "case_001", "CANCELLED")
        conn.commit()

        s = stats(conn)
        assert s["total"] == 5
        assert s["executed"] == 1
        assert s["failed"] == 1
        assert s["unknown"] == 1
        assert s["pending"] == 1
        assert s["cancelled"] == 1


class TestGetIntentsForCase:
    def test_returns_empty_when_no_intents(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        conn.commit()
        result = get_intents_for_case(conn, "case_001")
        assert result == []

    def test_returns_intents_newest_first(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        _insert_intent(conn, "i001", "case_001", "EXECUTED", created_at="2026-08-24T00:00:00+00:00")
        _insert_intent(conn, "i002", "case_001", "UNKNOWN", created_at="2026-08-24T01:00:00+00:00")
        _insert_intent(conn, "i003", "case_001", "PENDING", created_at="2026-08-24T02:00:00+00:00")
        conn.commit()

        result = get_intents_for_case(conn, "case_001")
        assert len(result) == 3
        assert result[0].intent_id == "i003"   # newest first
        assert result[1].intent_id == "i002"
        assert result[2].intent_id == "i001"

    def test_intent_fields_populated(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001", order_id="order_test")
        _insert_intent(conn, "i001", "case_001", "EXECUTED",
                       action_type="RETRY", provider_ref="ref_abc123",
                       created_at="2026-08-24T00:00:00+00:00")
        conn.commit()

        [intent] = get_intents_for_case(conn, "case_001")
        assert intent.intent_id == "i001"
        assert intent.case_id == "case_001"
        assert intent.order_id == "order_test"
        assert intent.status == IntentStatus.EXECUTED
        assert intent.action_type == "RETRY"
        assert intent.external_ref == "ref_abc123"
        assert intent.is_reconciled is True

    def test_missing_order_id_is_none(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001", order_id=None)
        _insert_intent(conn, "i001", "case_001", "PENDING")
        conn.commit()

        [intent] = get_intents_for_case(conn, "case_001")
        assert intent.order_id is None


class TestGetPendingIntents:
    def test_excludes_unknown_when_too_recent(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        recent = datetime.now(timezone.utc).isoformat()
        _insert_intent(conn, "i001", "case_001", "UNKNOWN", created_at=recent)
        conn.commit()

        result = get_pending_intents(conn, before_minutes=30)
        assert result == []

    def test_includes_unknown_when_old_enough(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        _insert_intent(conn, "i001", "case_001", "UNKNOWN", created_at=old)
        conn.commit()

        result = get_pending_intents(conn, before_minutes=30)
        assert len(result) == 1
        assert result[0].intent_id == "i001"
        assert result[0].status == IntentStatus.UNKNOWN

    def test_includes_pending_old_enough(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        _insert_intent(conn, "i001", "case_001", "PENDING", created_at=old)
        conn.commit()

        result = get_pending_intents(conn, before_minutes=30)
        assert len(result) == 1
        assert result[0].status == IntentStatus.PENDING

    def test_excludes_executed_and_failed(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        _insert_intent(conn, "i001", "case_001", "EXECUTED", created_at=old)
        _insert_intent(conn, "i002", "case_001", "FAILED", created_at=old)
        _insert_intent(conn, "i003", "case_001", "CANCELLED", created_at=old)
        conn.commit()

        result = get_pending_intents(conn, before_minutes=30)
        assert result == []

    def test_orders_by_created_at_ascending(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        t1 = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        t2 = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        t3 = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        _insert_intent(conn, "i003", "case_001", "UNKNOWN", created_at=t3)
        _insert_intent(conn, "i001", "case_001", "UNKNOWN", created_at=t1)
        _insert_intent(conn, "i002", "case_001", "PENDING", created_at=t2)
        conn.commit()

        result = get_pending_intents(conn, before_minutes=30)
        assert [r.intent_id for r in result] == ["i001", "i002", "i003"]

    def test_empty_when_no_intents(self):
        conn = _fresh_db()
        result = get_pending_intents(conn, before_minutes=30)
        assert result == []


class TestResolve:
    def test_resolve_sets_provider_ref(self):
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        _insert_intent(conn, "i001", "case_001", "UNKNOWN")
        conn.commit()

        success = resolve(conn, "i001", "ref_xyz")
        assert success is True

        [intent] = get_intents_for_case(conn, "case_001")
        assert intent.external_ref == "ref_xyz"
        assert intent.is_reconciled is True

    def test_resolve_unknown_not_executed(self):
        """resolve() only sets provider_ref; status is set by act._settle()."""
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        _insert_intent(conn, "i001", "case_001", "UNKNOWN")
        conn.commit()

        resolve(conn, "i001", "ref_abc")
        [intent] = get_intents_for_case(conn, "case_001")
        # Status remains UNKNOWN after resolve; act._settle() changes it
        assert intent.status == IntentStatus.UNKNOWN

    def test_resolve_returns_false_when_not_found(self):
        conn = _fresh_db()
        success = resolve(conn, "nonexistent", "ref_123")
        assert success is False

    def test_resolve_does_not_affect_executed(self):
        """An already-settled intent is not overwritten by resolve."""
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        _insert_intent(conn, "i001", "case_001", "EXECUTED", provider_ref="original_ref")
        conn.commit()

        success = resolve(conn, "i001", "new_ref")
        assert success is False   # rowcount=0 because status not in (UNKNOWN,PENDING)

        [intent] = get_intents_for_case(conn, "case_001")
        assert intent.external_ref == "original_ref"   # unchanged

    def test_resolve_multiple_unknowns(self):
        """All UNKNOWN intents can be resolved independently."""
        conn = _fresh_db()
        _insert_case(conn, "case_001")
        _insert_intent(conn, "i001", "case_001", "UNKNOWN")
        _insert_intent(conn, "i002", "case_001", "UNKNOWN")
        conn.commit()

        resolve(conn, "i001", "ref_001")
        resolve(conn, "i002", "ref_002")

        intents = get_intents_for_case(conn, "case_001")
        by_id = {i.intent_id: i for i in intents}
        assert by_id["i001"].external_ref == "ref_001"
        assert by_id["i002"].external_ref == "ref_002"
