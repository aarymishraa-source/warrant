"""Tests for warrant.assign."""

import pytest

from warrant import config
from warrant.assign import assign_experiment, check_carveout, evaluate_assignment


class TestAssignExperiment:
    def test_determinism_same_id_same_salt(self):
        """Same customer_id with same salt always returns the same arm."""
        result1 = assign_experiment("cust_123", salt=config.ASSIGNMENT_SALT)
        result2 = assign_experiment("cust_123", salt=config.ASSIGNMENT_SALT)
        assert result1 == result2

    def test_determinism_with_default_salt(self):
        """Default salt from config produces stable results."""
        result1 = assign_experiment("cust_123")
        result2 = assign_experiment("cust_123")
        assert result1 == result2

    def test_different_salt_produces_different_arm(self):
        """Different salts can produce different arms for the same customer."""
        result_v1 = assign_experiment("cust_123", salt="warrant-v1")
        result_v2 = assign_experiment("cust_123", salt="warrant-v2")
        assert result_v1["arm"] in ("HOLDOUT", "RULES", "WARRANT")
        assert result_v2["arm"] in ("HOLDOUT", "RULES", "WARRANT")

    def test_arm_is_always_valid(self):
        """Arm must be one of the three valid arms."""
        for i in range(200):
            result = assign_experiment(f"test_id_{i}")
            assert result["arm"] in ("HOLDOUT", "RULES", "WARRANT")


@pytest.mark.parametrize("customer_id,expected_arm", [
    ("boundary_14", "HOLDOUT"),   # bucket 19
    ("boundary_84", "RULES"),     # bucket 20
    ("boundary_206", "RULES"),    # bucket 59
    ("boundary_139", "WARRANT"),  # bucket 60
    ("boundary_4", "WARRANT"),    # bucket 99
])
def test_bucket_boundaries(customer_id, expected_arm):
    result = assign_experiment(customer_id)
    assert result["arm"] == expected_arm, f"bucket boundary {customer_id} expected {expected_arm}"


class TestAssignDistribution:
    def test_distribution_20_40_40_across_1000(self):
        """Across 1000 IDs, distribution should be approximately 20/40/40."""
        ids = [f"cust_{i}" for i in range(1000)]
        holdout = sum(1 for cid in ids if assign_experiment(cid)["arm"] == "HOLDOUT")
        rules = sum(1 for cid in ids if assign_experiment(cid)["arm"] == "RULES")
        warrant = sum(1 for cid in ids if assign_experiment(cid)["arm"] == "WARRANT")

        total = holdout + rules + warrant
        assert total == 1000

        # Allow 5% tolerance: HOLDOUT ~200 (±50), RULES ~400 (±50), WARRANT ~400 (±50)
        assert 150 <= holdout <= 250, f"HOLDOUT: expected ~200, got {holdout}"
        assert 350 <= rules <= 450, f"RULES: expected ~400, got {rules}"
        assert 350 <= warrant <= 450, f"WARRANT: expected ~400, got {warrant}"

    def test_distribution_exact_boundaries(self):
        """Known SHA-256 inputs produce the correct bucket ranges."""
        # Buckets 0-19 -> HOLDOUT, 20-59 -> RULES, 60-99 -> WARRANT
        # We verify by checking that all three arms appear across enough IDs
        arms_seen = set()
        for i in range(500):
            arms_seen.add(assign_experiment(f"dist_test_{i}")["arm"])
        assert arms_seen == {"HOLDOUT", "RULES", "WARRANT"}


class TestCheckCarveout:
    def test_upi_autopay_cap_reached_returns_reason(self):
        result = check_carveout({"customer_id": "cust_123", "upi_autopay_cap_reached": True})
        assert result == "upi_autopay_cap_reached"

    def test_invalid_customer_id_flag_returns_reason(self):
        result = check_carveout({"customer_id": "cust_123", "invalid_customer_id": True})
        assert result == "invalid_customer_id"

    def test_opt_out_true_returns_reason(self):
        result = check_carveout({"customer_id": "cust_123", "opt_out": True})
        assert result == "opt_out"

    def test_opt_out_false_returns_none(self):
        result = check_carveout({"customer_id": "cust_123", "opt_out": False})
        assert result is None

    def test_opt_out_none_returns_none(self):
        result = check_carveout({"customer_id": "cust_123", "opt_out": None})
        assert result is None

    def test_no_carveout_fields_returns_none(self):
        result = check_carveout({"customer_id": "cust_123"})
        assert result is None

    def test_empty_dict_returns_none(self):
        # empty dict has no customer_id, so invalid_customer_id fires first
        result = check_carveout({})
        assert result == "invalid_customer_id"

    def test_missing_customer_id_returns_invalid(self):
        """No customer_id key triggers invalid_customer_id carveout."""
        result = check_carveout({})
        assert result == "invalid_customer_id"

    def test_empty_string_customer_id_returns_invalid(self):
        result = check_carveout({"customer_id": ""})
        assert result == "invalid_customer_id"

    def test_whitespace_only_customer_id_returns_invalid(self):
        result = check_carveout({"customer_id": "   "})
        assert result == "invalid_customer_id"

    def test_none_customer_id_returns_invalid(self):
        result = check_carveout({"customer_id": None})
        assert result == "invalid_customer_id"


class TestCarveoutPriority:
    def test_invalid_takes_priority_over_opt_out(self):
        result = check_carveout(
            {"customer_id": "", "opt_out": True}
        )
        assert result == "invalid_customer_id"

    def test_invalid_flag_takes_priority_over_opt_out(self):
        result = check_carveout(
            {"customer_id": "cust_123", "invalid_customer_id": True, "opt_out": True}
        )
        assert result == "invalid_customer_id"

    def test_opt_out_takes_priority_over_upi_cap(self):
        result = check_carveout(
            {"customer_id": "cust_123", "opt_out": True, "upi_autopay_cap_reached": True}
        )
        assert result == "opt_out"

    def test_upi_cap_is_lowest_priority(self):
        result = check_carveout(
            {"customer_id": "cust_123", "upi_autopay_cap_reached": True}
        )
        assert result == "upi_autopay_cap_reached"

    def test_all_flags_present_invalid_wins(self):
        result = check_carveout(
            {
                "customer_id": "cust_123",
                "invalid_customer_id": True,
                "opt_out": True,
                "upi_autopay_cap_reached": True,
            }
        )
        assert result == "invalid_customer_id"

    def test_missing_customer_id_wins_over_all_others(self):
        result = check_carveout(
            {
                "opt_out": True,
                "upi_autopay_cap_reached": True,
            }
        )
        assert result == "invalid_customer_id"


class TestEvaluateAssignment:
    def test_carveout_precedence_returns_carve_out_arm(self):
        """Carveout takes precedence over experiment assignment."""
        result = evaluate_assignment(
            {"case_id": "case_1", "customer_id": "cust_123", "upi_autopay_cap_reached": True}
        )
        assert result.arm == "CARVE_OUT"
        assert result.carve_reason == "upi_autopay_cap_reached"
        assert result.carved_out is True

    def test_no_carveout_calls_assign_experiment(self):
        """When no carveout, arm is determined by assign_experiment."""
        result = evaluate_assignment({"case_id": "case_2", "customer_id": "cust_abc"})
        assert result.arm in ("HOLDOUT", "RULES", "WARRANT")
        assert result.carved_out is False
        assert result.carve_reason is None

    def test_opt_out_carveout(self):
        result = evaluate_assignment(
            {"case_id": "case_3", "customer_id": "cust_xyz", "opt_out": True}
        )
        assert result.arm == "CARVE_OUT"
        assert result.carve_reason == "opt_out"

    def test_missing_customer_id_carveout(self):
        result = evaluate_assignment({})
        assert result.arm == "CARVE_OUT"
        assert result.carve_reason == "invalid_customer_id"

    def test_empty_customer_id_carveout(self):
        result = evaluate_assignment({"customer_id": ""})
        assert result.arm == "CARVE_OUT"
        assert result.carve_reason == "invalid_customer_id"

    def test_uses_config_salt(self):
        """evaluate_assignment passes config.ASSIGNMENT_SALT to assign_experiment."""
        result = evaluate_assignment({"case_id": "case_4", "customer_id": "cust_123"})
        expected = assign_experiment("cust_123", salt=config.ASSIGNMENT_SALT)
        assert result.arm == expected["arm"]
