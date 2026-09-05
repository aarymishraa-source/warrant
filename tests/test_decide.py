"""Tests for warrant.decide."""

from warrant.config import LLM_CONFIDENCE_FLOOR
from warrant.decide import (
    CaseData,
    compare_proposers,
    llm_propose,
    resolve_proposal,
    rules_propose,
)

# ------------------------------------------------------------------------- helpers

def case(
    error_reason=None, error_source=None, error_step=None,
    case_type="one_time_link", ticket_paise=10_000,
) -> CaseData:
    return CaseData(
        case_id="case_test_001",
        customer_id="cust_test_001",
        case_type=case_type,
        ticket_amount_paise=ticket_paise,
        error_reason=error_reason,
        error_source=error_source,
        error_step=error_step,
    )


# ------------------------------------------------------------------------- rules proposer

class TestRulesPropose:
    def test_no_structured_fields_one_time_link(self):
        result = rules_propose(case(error_reason=None))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"
        assert result.source == "rules"

    def test_no_structured_fields_upi_autopay(self):
        result = rules_propose(case(error_reason=None, case_type="upi_autopay"))
        assert result.abstained is False
        assert result.proposal.action == "RETRY"
        assert result.source == "rules"

    def test_insufficient_balance_one_time_link(self):
        result = rules_propose(case(error_reason="INSUFFICIENT_BALANCES"))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_insufficient_balance_upi_autopay(self):
        result = rules_propose(case(
            error_reason="INSUFFICIENT_BALANCES", case_type="upi_autopay"
        ))
        assert result.abstained is True
        assert "insufficient balance" in result.abstention_reason

    def test_card_declined_one_time_link(self):
        result = rules_propose(case(error_reason="CARD_DECLINED"))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_bank_declined_one_time_link(self):
        result = rules_propose(case(error_reason="BANK_DECLINED"))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_auth_failure_one_time_link(self):
        result = rules_propose(case(
            error_reason="PAYMENT_AUTHENTICATION_FAILED",
            error_step="payment_authentication",
        ))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_auth_failure_upi_autopay(self):
        result = rules_propose(case(
            error_reason="PAYMENT_AUTHENTICATION_FAILED",
            error_step="payment_authentication",
            case_type="upi_autopay",
        ))
        assert result.abstained is False
        assert result.proposal.action == "RETRY"

    def test_network_error(self):
        result = rules_propose(case(error_reason="NETWORK_ERROR"))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_network_error_upi_autopay(self):
        result = rules_propose(case(
            error_reason="NETWORK_ERROR", case_type="upi_autopay"
        ))
        assert result.abstained is False
        assert result.proposal.action == "RETRY"

    def test_bad_request_one_time_link(self):
        result = rules_propose(case(error_reason="BAD_REQUEST_ERROR"))
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_unknown_error_abstains(self):
        result = rules_propose(case(error_reason="UNKNOWN_ERROR_XYZ"))
        assert result.abstained is True
        assert result.source == "rules"

    def test_unknown_error_preserves_detail(self):
        result = rules_propose(case(error_reason="UNKNOWN_ERROR_XYZ"))
        assert "UNKNOWN_ERROR_XYZ" in result.abstention_reason

    def test_rules_returns_confidence(self):
        result = rules_propose(case(error_reason=None))
        assert result.proposal is not None
        assert 0.0 <= result.proposal.confidence <= 1.0

    def test_case_type_scoped_rules(self):
        # Same error, different case types -> different outcomes
        r1 = rules_propose(case(error_reason="INSUFFICIENT_BALANCES", case_type="one_time_link"))
        r2 = rules_propose(case(error_reason="INSUFFICIENT_BALANCES", case_type="upi_autopay"))
        assert r1.proposal.action == "SEND_PAYMENT_LINK"
        assert r2.abstained is True

    def test_rationale_contains_action(self):
        result = rules_propose(case(error_reason="CARD_DECLINED"))
        assert result.proposal is not None
        assert len(result.proposal.rationale) > 10


# ------------------------------------------------------------------------- LLM proposer

class TestLLMPropose:
    def test_valid_output_returns_proposal(self):
        result = llm_propose(
            case(error_reason="INSUFFICIENT_BALANCES"),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Card insufficient funds; link gives customer time to fund account.",
                "confidence": 0.92,
            },
        )
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"
        assert result.source == "llm"

    def test_missing_llm_output_abstains(self):
        result = llm_propose(case(), llm_output=None)
        assert result.abstained is True
        assert "not configured" in result.abstention_reason

    def test_missing_action_field_abstains(self):
        result = llm_propose(
            case(),
            llm_output={"timing": "immediate", "channel": "sms", "confidence": 0.9},
        )
        assert result.abstained is True

    def test_missing_confidence_field_abstains(self):
        result = llm_propose(
            case(),
            llm_output={"action": "SEND_PAYMENT_LINK", "timing": "immediate", "channel": "sms"},
        )
        assert result.abstained is True

    def test_unknown_action_abstains(self):
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_DISCOUNT_CODE",
                "timing": "immediate",
                "channel": "sms",
                "confidence": 0.9,
            },
        )
        assert result.abstained is True

    def test_confidence_below_floor_abstains(self):
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "confidence": LLM_CONFIDENCE_FLOOR - 0.01,
            },
        )
        assert result.abstained is True

    def test_confidence_at_floor_accepted(self):
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Test rationale.",
                "confidence": LLM_CONFIDENCE_FLOOR,
            },
        )
        assert result.abstained is False

    def test_confidence_above_floor_accepted(self):
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Test rationale.",
                "confidence": 0.99,
            },
        )
        assert result.abstained is False

    def test_extra_fields_stripped(self):
        """Extra fields in LLM output must not cause rejection."""
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Fresh link.",
                "confidence": 0.9,
                "extra_field": "ignored",
                "another_extra": 42,
            },
        )
        assert result.abstained is False

    def test_confidence_out_of_range_rejected(self):
        # > 1.0
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "confidence": 1.5,
            },
        )
        assert result.abstained is True

        # < 0.0
        result2 = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "confidence": -0.1,
            },
        )
        assert result2.abstained is True

    def test_unknown_channel_accepted(self):
        """Channel is not validated; policy engine handles invalid channels."""
        result = llm_propose(
            case(),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "pigeon",
                "rationale": "Test rationale.",
                "confidence": 0.9,
            },
        )
        # Not validated here; channel is policy-gated downstream
        assert result.abstained is False


# ------------------------------------------------------------------------- compare_proposers

class TestCompareProposers:
    def test_both_propose_same_action(self):
        result = compare_proposers(
            case(error_reason="CARD_DECLINED"),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Card declined; link recommended.",
                "confidence": 0.9,
            },
        )
        assert result["rules"].proposal.action == result["llm"].proposal.action
        assert result["rules"].proposal.action == "SEND_PAYMENT_LINK"

    def test_llm_abstains_rules_delivers(self):
        result = compare_proposers(
            case(error_reason="INSUFFICIENT_BALANCES"),
            llm_output=None,  # LLM abstains
        )
        assert result["llm"].abstained is True
        assert result["rules"].abstained is False
        assert result["rules"].proposal is not None

    def test_rules_abstains_llm_delivers(self):
        result = compare_proposers(
            case(error_reason="OBSCURE_UNKNOWN_ERROR"),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Fallback action.",
                "confidence": 0.85,
            },
        )
        assert result["rules"].abstained is True
        assert result["llm"].abstained is False

    def test_both_abstain(self):
        result = compare_proposers(
            case(error_reason="UNKNOWN_ERROR"),
            llm_output=None,
        )
        assert result["rules"].abstained is True
        assert result["llm"].abstained is True


# ------------------------------------------------------------------------- resolve_proposal

class TestResolveProposal:
    def test_llm_wins_when_confident(self):
        result = resolve_proposal(
            case(error_reason="CARD_DECLINED"),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Card declined.",
                "confidence": 0.95,
            },
        )
        assert result.source == "llm"
        assert result.abstained is False

    def test_falls_back_to_rules_when_llm_abstains(self):
        result = resolve_proposal(
            case(error_reason="INSUFFICIENT_BALANCES"),
            llm_output={
                "action": "SEND_PAYMENT_LINK",
                "timing": "immediate",
                "channel": "sms",
                "rationale": "Test.",
                "confidence": LLM_CONFIDENCE_FLOOR - 0.01,
            },
        )
        assert result.source == "rules"
        assert result.abstained is False  # rules proposes SEND_PAYMENT_LINK for one_time_link

    def test_rules_fallback_produces_proposal(self):
        result = resolve_proposal(
            case(error_reason="CARD_DECLINED"),
            llm_output=None,
        )
        assert result.source == "rules"
        assert result.abstained is False
        assert result.proposal.action == "SEND_PAYMENT_LINK"

    def test_resolve_never_returns_null_proposal_unless_both_abstain(self):
        result = resolve_proposal(
            case(error_reason="UNKNOWN_XYZ"),
            llm_output=None,
        )
        assert result.abstained is True
        assert result.proposal is None


# ------------------------------------------------------------------------- CaseData construction

class TestCaseData:
    def test_minimal_case(self):
        c = CaseData(
            case_id="c1", customer_id="u1",
            case_type="one_time_link", ticket_amount_paise=5000,
        )
        assert c.error_reason is None
        assert c.error_description is None

    def test_full_case(self):
        c = CaseData(
            case_id="c1", customer_id="u1",
            case_type="upi_autopay", ticket_amount_paise=50_000,
            error_reason="CARD_DECLINED",
            error_source="customer",
            error_step="payment_authentication",
            error_description="Card issuer declined the transaction",
        )
        assert c.error_reason == "CARD_DECLINED"
        assert c.case_type == "upi_autopay"
