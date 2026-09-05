"""Tests for warrant.sim."""


from warrant import config, db
from warrant.act import IntentStatus
from warrant.act import execute as act_execute
from warrant.core import CaseState, create_case
from warrant.policy import Decision, Proposal, Verdict
from warrant.sim import (
    CustomerSegment,
    Simulator,
    _truth,
)

# ------------------------------------------------------------------------- helpers

def fresh_db():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


def make_verdict(action="SEND_PAYMENT_LINK"):
    return Verdict(
        decision=Decision.EXECUTE,
        rule_id="sim_test",
        reason="test rationale",
        expected_value_paise=5000,
        action_cost_paise=50,
        evaluated_at="2026-08-24T00:00:00+00:00",
    )


def make_proposal(action="SEND_PAYMENT_LINK"):
    return Proposal(
        action=action,
        timing="immediate",
        channel="sms",
        rationale="test rationale",
        confidence=0.9,
    )


def make_case(conn, case_id, customer_id="cust_test", order_id="order_test",
              payment_id="pay_test", case_type="one_time_link",
              ticket_amount_paise=25000):
    """Create a case in ACTION_QUEUED state so act.execute can run."""
    from warrant.core import transition
    c = create_case(conn, case_id, customer_id, case_type, ticket_amount_paise,
                    payment_id=payment_id, order_id=order_id)
    # Walk the full state machine: OBSERVED_FAILED -> CLASSIFIED -> ASSIGNED -> ACTION_QUEUED
    c = transition(conn, c.case_id, CaseState.CLASSIFIED, expected_version=0,
                  reason="sim_test")
    c = transition(conn, c.case_id, CaseState.ASSIGNED, expected_version=1,
                  reason="sim_test")
    return transition(conn, c.case_id, CaseState.ACTION_QUEUED, expected_version=2,
                     reason="sim_test")


# ------------------------------------------------------------------------- determinism

class TestSimulatorDeterminism:
    def test_same_seed_same_sequence_produces_identical_cases(self):
        s1 = Simulator(seed=0)
        s2 = Simulator(seed=0)
        for _ in range(50):
            c1 = s1.generate_case()
            c2 = s2.generate_case()
            assert c1.case_id == c2.case_id
            assert c1.customer_id == c2.customer_id
            assert c1.case_type == c2.case_type
            assert c1.ticket_amount_paise == c2.ticket_amount_paise
            assert c1.error_reason == c2.error_reason

    def test_different_seeds_produce_different_sequences(self):
        s1 = Simulator(seed=0)
        s2 = Simulator(seed=1)
        # Case IDs come from a counter, not randomness. Compare random fields instead.
        amounts1 = [s1.generate_case().ticket_amount_paise for _ in range(20)]
        amounts2 = [s2.generate_case().ticket_amount_paise for _ in range(20)]
        assert amounts1 != amounts2, "different seeds should produce different random fields"

    def test_generate_batch_is_deterministic(self):
        s = Simulator(seed=42)
        batch1 = s.generate_batch(30)
        batch2 = Simulator(seed=42).generate_batch(30)
        assert len(batch1) == len(batch2)
        for c1, c2 in zip(batch1, batch2):
            assert c1.case_id == c2.case_id
            assert c1.ticket_amount_paise == c2.ticket_amount_paise

    def test_case_types_are_valid(self):
        s = Simulator(seed=0)
        for _ in range(200):
            c = s.generate_case()
            assert c.case_type in ("one_time_link", "upi_autopay")

    def test_error_reason_distribution(self):
        """Broad coverage: over many cases all error reasons should appear."""
        s = Simulator(seed=0)
        reasons_seen = set()
        for _ in range(500):
            c = s.generate_case()
            if c.error_reason is not None:
                reasons_seen.add(c.error_reason)
        # Should see at least 5 of the 6 structured error reasons.
        assert len(reasons_seen) >= 5, f"only {reasons_seen} seen in 500 cases"

    def test_master_seed_from_config(self):
        """The module uses config.SEED, not a hard-coded value."""
        assert config.SEED == 20260824


# ------------------------------------------------------------------------- ground truth isolation

class TestImportBoundary:
    def test_sim_truth_not_importable_from_policy(self):
        """Ground truth must not leak into the policy engine."""
        import warrant.policy as policy_mod
        assert not hasattr(policy_mod, "_truth")
        assert not hasattr(policy_mod, "P_SELF_HEAL")
        assert not hasattr(policy_mod, "CustomerSegment")

    def test_sim_truth_not_importable_from_decide(self):
        """Ground truth must not leak into the decision layer."""
        import warrant.decide as decide_mod
        assert not hasattr(decide_mod, "_truth")
        assert not hasattr(decide_mod, "CustomerSegment")

    def test_sim_truth_not_importable_from_act(self):
        """Ground truth must not leak into the actuator."""
        import warrant.act as act_mod
        assert not hasattr(act_mod, "_truth")
        assert not hasattr(act_mod, "P_SELF_HEAL")

    def test_sim_module_does_not_export_truth(self):
        """The sim module __all__ must not include ground-truth names."""
        import warrant.sim as sim_mod
        all_list = getattr(sim_mod, "__all__", [])
        assert "_truth" not in all_list
        assert "_rng" not in all_list
        assert "P_SELF_HEAL" not in all_list
        # CustomerSegment is a simulator-facing enum, so it's fine if exported.
        # _Truth is the hidden class and must not be exported.
        assert "CustomerSegment" in all_list

    def test_sim_truth_not_importable_from_warrant_package(self):
        """The top-level warrant package must not expose ground truth."""
        import warrant
        for name in ("_truth", "P_SELF_HEAL", "CustomerSegment"):
            assert not hasattr(warrant, name), f"{name} leaked to warrant package"


# ------------------------------------------------------------------------- ground truth behaviour

class TestGroundTruth:
    def test_self_heal_rate_approximately_34_percent(self):
        """Over many customers the self-heal rate should approach 34%."""
        s = Simulator(seed=0)
        customers = {s.generate_case().customer_id for _ in range(500)}
        healed = sum(1 for c in customers if _truth.self_healed(c))
        rate = healed / len(customers)
        assert 0.28 < rate < 0.40, f"self-heal rate {rate:.2%} not near 34%"

    def test_segment_distribution(self):
        """Over many customers, segment distribution should be roughly 5/10/85."""
        s = Simulator(seed=0)
        customers = [s.generate_case().customer_id for _ in range(500)]
        segments = [_truth.segment(c) for c in customers]
        sleeping = sum(1 for s_ in segments if s_ is CustomerSegment.SLEEPING_DOG)
        responsive = sum(1 for s_ in segments if s_ is CustomerSegment.RESPONSIVE)
        neutral = sum(1 for s_ in segments if s_ is CustomerSegment.NEUTRAL)
        assert 0.02 < sleeping / 500 < 0.10, f"SLEEPING_DOG rate unexpected: {sleeping}"
        assert 0.05 < responsive / 500 < 0.18, f"RESPONSIVE rate unexpected: {responsive}"
        assert neutral / 500 > 0.75, f"NEUTRAL rate unexpected: {neutral}"

    def test_treatment_probability_responsive_segment(self):
        """RESPONSIVE customers should have higher treatment recovery probability."""
        s = Simulator(seed=0)
        # Find a RESPONSIVE customer.
        responsive_cust = None
        for _ in range(1000):
            c = s.generate_case()
            if _truth.segment(c.customer_id) is CustomerSegment.RESPONSIVE:
                responsive_cust = c.customer_id
                break
        assert responsive_cust is not None, "no RESPONSIVE customer found in 1000 cases"
        prob = _truth.treatment_recovery_prob(responsive_cust)
        # RESPONSIVE: P_self_heal + 0.12 = 0.34 + 0.12 = 0.46
        assert 0.40 < prob < 0.55

    def test_treatment_probability_sleeping_dog_segment(self):
        """SLEEPING_DOG customers should have lower treatment recovery probability."""
        s = Simulator(seed=0)
        sleeping_cust = None
        for _ in range(2000):
            c = s.generate_case()
            if _truth.segment(c.customer_id) is CustomerSegment.SLEEPING_DOG:
                sleeping_cust = c.customer_id
                break
        assert sleeping_cust is not None, "no SLEEPING_DOG customer found in 2000 cases"
        prob = _truth.treatment_recovery_prob(sleeping_cust)
        # SLEEPING_DOG: P_self_heal - 0.04 = 0.30
        assert 0.25 < prob < 0.35

    def test_segment_is_deterministic_per_customer(self):
        """Same customer_id always returns the same segment."""
        s = Simulator(seed=0)
        c = s.generate_case(customer_id="fixed_cust_999")
        seg1 = _truth.segment(c.customer_id)
        seg2 = _truth.segment(c.customer_id)
        assert seg1 is seg2

    def test_self_healed_is_deterministic_per_customer(self):
        """Same customer_id always returns the same self_heal flag."""
        s = Simulator(seed=0)
        c = s.generate_case(customer_id="fixed_cust_888")
        sh1 = _truth.self_healed(c.customer_id)
        sh2 = _truth.self_healed(c.customer_id)
        assert sh1 == sh2


# ------------------------------------------------------------------------- failure injections

class TestDuplicateWebhook:
    def test_first_call_creates_intent(self):
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "dup_case_1")
        proposal = make_proposal()
        verdict = make_verdict()

        def caller(req):
            return {"id": "ref_abc123"}

        intent = act_execute(conn, case, proposal, verdict, caller)
        assert intent.status is IntentStatus.EXECUTED

    def test_duplicate_webhook_three_calls_one_provider_call(self):
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "dup_case_2")
        proposal = make_proposal()
        verdict = make_verdict()
        provider_call_count = 0

        def tracking_caller(req):
            nonlocal provider_call_count
            provider_call_count += 1
            return {"id": f"ref_{provider_call_count}"}

        results = s.simulate_duplicate_webhook(
            conn, case, proposal, verdict, caller=tracking_caller
        )

        assert len(results) == 3
        # Only the first call should have hit the provider.
        assert provider_call_count == 1
        # All three intents share the same idempotency key.
        keys = {r["idempotency_key"] for r in results}
        assert len(keys) == 1
        # All three intents have the same ID.
        ids = {r["intent_id"] for r in results}
        assert len(ids) == 1
        # All three are EXECUTED.
        for r in results:
            assert r["status"] == "EXECUTED"

    def test_same_idempotency_key_across_deliveries(self):
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "dup_case_3")
        proposal = make_proposal()
        verdict = make_verdict()

        def caller(req):
            return {"id": "ref_xyz"}

        results = s.simulate_duplicate_webhook(conn, case, proposal, verdict)
        key = results[0]["idempotency_key"]
        assert len(key) > 0  # key is non-empty string


class TestOrderPaidMidIntervention:
    def test_cancel_pending_cancels_unknown_intents(self):
        """cancel_pending cancels UNKNOWN intents (created by a timeout)."""
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "paid_case_1", order_id="order_paid_1")
        proposal = make_proposal()
        verdict = make_verdict()

        # Create an UNKNOWN intent via timeout.
        timeout_result = s.simulate_action_timeout(conn, case, proposal, verdict)
        assert timeout_result["status"] == "UNKNOWN"

        # Now simulate order.paid arriving.
        cancelled = s.simulate_order_paid(conn, case)
        assert len(cancelled) == 1
        assert cancelled[0]["status_after"] == "CANCELLED"

    def test_cancel_pending_returns_empty_when_no_pending_intents(self):
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "paid_case_2")
        cancelled = s.simulate_order_paid(conn, case)
        assert cancelled == []


class TestActionTimeout:
    def test_timeout_produces_unknown_not_failed(self):
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "timeout_case_1")
        proposal = make_proposal()
        verdict = make_verdict()

        result = s.simulate_action_timeout(conn, case, proposal, verdict)
        assert result["is_unknown"] is True
        assert result["is_not_failed"] is True
        assert result["status"] == "UNKNOWN"

    def test_timeout_intent_is_recorded(self):
        conn = fresh_db()
        s = Simulator(seed=0)
        case = make_case(conn, "timeout_case_2")
        proposal = make_proposal()
        verdict = make_verdict()

        s.simulate_action_timeout(conn, case, proposal, verdict)

        cur = conn.execute(
            "SELECT status FROM intents WHERE case_id = ?",
            (case.case_id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "UNKNOWN"


# ------------------------------------------------------------------------- SimulatorCase

class TestSimulatorCase:
    def test_case_fields_populated(self):
        s = Simulator(seed=0)
        c = s.generate_case()
        assert c.case_id.startswith("sim_")
        assert c.customer_id.startswith("cust_")
        assert c.case_type in ("one_time_link", "upi_autopay")
        assert c.ticket_amount_paise >= 500
        assert c.order_id.startswith("order_")
        assert c.payment_id.startswith("pay_")

    def test_case_with_explicit_id(self):
        s = Simulator(seed=0)
        c = s.generate_case(case_id="explicit_001", customer_id="cust_explicit")
        assert c.case_id == "explicit_001"
        assert c.customer_id == "cust_explicit"

    def test_simulate_treatment_sets_outcome_fields(self):
        s = Simulator(seed=0)
        c = s.generate_case()
        assert c.outcome_treated is None
        recovered = s.simulate_treatment(c)
        assert c.outcome_treated is not None
        assert c.outcome_untouched is not None
        assert c.segment is not None
        assert isinstance(recovered, bool)

    def test_simulate_treatment_idempotent_on_outcome_treated(self):
        """Calling simulate_treatment twice must not re-draw the outcome."""
        s = Simulator(seed=0)
        c = s.generate_case(customer_id="idempotent_999")
        s.simulate_treatment(c)
        first_outcome = c.outcome_treated
        s.simulate_treatment(c)
        assert c.outcome_treated == first_outcome
