"""LLM proposer and rules-only baseline for the decision layer.

Two proposers feed the policy engine:
  RulesProposer  — deterministic lookup table on structured error fields
  LLMProposer    — Pydantic-validated model output, abstains on low confidence

Both return ProposerResult.  Abstention means the controller records DO_NOTHING
and logs the reason.

The LLM never computes EV, calls a payment API, or overrides a gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from warrant.config import ACTION_COST_INR, LLM_CONFIDENCE_FLOOR
from warrant.policy import Proposal


# ------------------------------------------------------------------------- types

@dataclass
class CaseData:
    """Structured payment failure data passed to the proposer."""

    case_id: str
    customer_id: str
    case_type: str          # "upi_autopay" | "one_time_link"
    ticket_amount_paise: int
    error_reason: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_description: Optional[str] = None  # free text; LLM only


@dataclass
class ProposerResult:
    """Unified output from any proposer."""

    proposal: Optional[Proposal]
    abstained: bool
    abstention_reason: Optional[str]
    source: str  # "rules" | "llm"


# ------------------------------------------------------------------------- rules proposer

# Sentinel for "no structured error fields" — distinct from None (wildcard).
# Placed here so _RULES can reference it at module level.
_UNSTRUCTURED = object()

# (error_reason, error_source, error_step, case_type) -> (action, confidence, rationale)
# _UNSTRUCTURED sentinel means "error_reason is None".
# None means "match any value".
_RULES = [
    # ---- STRUCTURED ABSTAIN (specific, checked first) ----
    (("INSUFFICIENT_BALANCES", None, None, "upi_autopay"), "DO_NOTHING", 0.80,
     "insufficient balance; retrying an empty account wastes the attempt cap"),
    (("BANK_DECLINED", None, None, "upi_autopay"), "DO_NOTHING", 0.75,
     "bank decline; autopay mandate cannot be forced; customer must resolve"),
    (("BAD_REQUEST_ERROR", None, None, "upi_autopay"), "DO_NOTHING", 0.70,
     "bad request on mandate; requires account correction, not retry"),

    # ---- STRUCTURED ACT (specific (reason, source, step, type)) ----
    (("PAYMENT_AUTHENTICATION_FAILED", None, "payment_authentication", "one_time_link"),
     "SEND_PAYMENT_LINK", 0.95,
     "auth expired; fresh link with new OTP window is the correct fix"),
    (("PAYMENT_AUTHENTICATION_FAILED", None, "payment_authentication", "upi_autopay"),
     "RETRY", 0.90,
     "auth failure on mandate; retry re-triggers the mandate auth sequence"),
    (("CARD_DECLINED", None, None, "one_time_link"), "SEND_PAYMENT_LINK", 0.90,
     "card declined; a new link with updated card details usually clears it"),
    (("CARD_DECLINED", None, None, "upi_autopay"), "RETRY", 0.80,
     "card decline; mandate retry with same card may succeed after auth refresh"),
    (("INSUFFICIENT_BALANCES", None, None, "one_time_link"), "SEND_PAYMENT_LINK", 0.88,
     "insufficient balance; link lets customer fund and retry at their pace"),
    (("BANK_DECLINED", None, None, "one_time_link"), "SEND_PAYMENT_LINK", 0.85,
     "bank declined; a UPI/NetBanking link bypasses the card"),
    (("BAD_REQUEST_ERROR", None, None, "one_time_link"), "SEND_PAYMENT_LINK", 0.82,
     "bad request; a corrected payment link with valid parameters is the fix"),
    (("NETWORK_ERROR", None, None, "one_time_link"), "SEND_PAYMENT_LINK", 0.88,
     "transient network error; retry with a fresh link is low-risk"),
    (("NETWORK_ERROR", None, None, "upi_autopay"), "RETRY", 0.85,
     "transient error; autopay retry is appropriate for intermittent failures"),

    # ---- REASON-ONLY rules (wildcard source, step, type) ----
    (("PAYMENT_AUTHENTICATION_FAILED", None, None, None), "SEND_PAYMENT_LINK", 0.88,
     "auth failure; new OTP window required"),
    (("CARD_DECLINED", None, None, None), "SEND_PAYMENT_LINK", 0.88,
     "card decline; alternative payment method via link"),
    (("BANK_DECLINED", None, None, None), "SEND_PAYMENT_LINK", 0.82,
     "bank decline; alternative method via link"),
    (("NETWORK_ERROR", None, None, None), "SEND_PAYMENT_LINK", 0.85,
     "transient network error; fresh link recommended"),
    (("INSUFFICIENT_BALANCES", None, None, None), "SEND_PAYMENT_LINK", 0.88,
     "insufficient balance; link lets customer fund and retry"),

    # ---- SOURCE-LEVEL rules (no reason, wildcard step, type) ----
    (("EXTERNAL_ERROR", "bank", None, "one_time_link"), "SEND_PAYMENT_LINK", 0.75,
     "bank-side error; link with alternative method recommended"),
    (("EXTERNAL_ERROR", "bank", None, "upi_autopay"), "RETRY", 0.70,
     "bank error on mandate; retry is appropriate for transient bank issues"),
    (("EXTERNAL_ERROR", "customer", None, "one_time_link"), "SEND_PAYMENT_LINK", 0.80,
     "customer-side error; a guided link resolves most cases"),
    (("EXTERNAL_ERROR", "customer", None, "upi_autopay"), "RETRY", 0.75,
     "customer-side error; autopay retry after correction is standard"),

    # ---- DEFAULT (no structured fields — _UNSTRUCTURED sentinel) ----
    ((_UNSTRUCTURED, None, None, "one_time_link"), "SEND_PAYMENT_LINK", 0.95,
     "no structured error fields; fresh payment link is the standard recovery path"),
    ((_UNSTRUCTURED, None, None, "upi_autopay"), "RETRY", 0.85,
     "no structured error fields; autopay retry is the standard recovery path"),
]


def _match(pattern: object, value: str | None) -> bool:
    """Match a rule pattern field against a case field value.

    _UNSTRUCTURED sentinel means "value must be None (no structured field)".
    None pattern means "match any value".
    String pattern means "exact case-insensitive match".
    """
    if pattern is _UNSTRUCTURED:
        return value is None
    if pattern is None:
        return True
    if value is None:
        return False
    return value.upper() == pattern.upper()


def rules_propose(case: CaseData) -> ProposerResult:
    """Deterministic lookup: structured error fields → action + confidence.

    Returns the first matching rule. Abstains if no rule matches.
    This is the rules-only baseline with no LLM dependency.
    """
    for (patterns, action, confidence, rationale) in _RULES:
        err_r, src, step, ctype = patterns
        if not _match(err_r, case.error_reason):
            continue
        if not _match(src, case.error_source):
            continue
        if not _match(step, case.error_step):
            continue
        if not _match(ctype, case.case_type):
            continue

        if action == "DO_NOTHING":
            return ProposerResult(
                proposal=None,
                abstained=True,
                abstention_reason=f"rules: {rationale}",
                source="rules",
            )

        return ProposerResult(
            proposal=Proposal(
                action=action,
                timing="immediate",
                channel="sms",
                rationale=rationale,
                confidence=confidence,
            ),
            abstained=False,
            abstention_reason=None,
            source="rules",
        )

    # No rule matched: abstain rather than guess
    return ProposerResult(
        proposal=None,
        abstained=True,
        abstention_reason=(
            f"rules: no rule for reason={case.error_reason!r}, "
            f"source={case.error_source!r}, step={case.error_step!r}"
        ),
        source="rules",
    )


# ------------------------------------------------------------------------- LLM proposer

class _LLMOutput(BaseModel):
    action: str = Field(description="SEND_PAYMENT_LINK | REMIND | RETRY | DO_NOTHING")
    timing: str = Field(description="immediate | delayed_1h | delayed_24h | delayed_48h")
    channel: str = Field(description="sms | email | whatsapp | push")
    rationale: str = Field(description="One sentence. No EV or cost references.")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 to 1.0")

    model_config = {"extra": "allow"}  # ignore extra fields the model adds


def _llm_call(raw: dict) -> Optional[Proposal]:
    """Validate raw LLM output, or return None on any failure."""
    try:
        validated = _LLMOutput.model_validate(raw)
    except ValidationError:
        return None

    if validated.confidence < LLM_CONFIDENCE_FLOOR:
        return None
    if validated.action not in ACTION_COST_INR:
        return None

    return Proposal(
        action=validated.action,
        timing=validated.timing,
        channel=validated.channel,
        rationale=validated.rationale,
        confidence=validated.confidence,
    )


def llm_propose(case: CaseData, llm_output: dict | None = None) -> ProposerResult:
    """Route a case to the LLM and return a validated Proposal or abstain.

    llm_output=None means LLM is not configured; returns abstention.
    """
    if llm_output is None:
        return ProposerResult(
            proposal=None,
            abstained=True,
            abstention_reason="llm: not configured",
            source="llm",
        )

    proposal = _llm_call(llm_output)
    if proposal is None:
        return ProposerResult(
            proposal=None,
            abstained=True,
            abstention_reason="llm: validation failed or confidence below floor",
            source="llm",
        )

    return ProposerResult(
        proposal=proposal,
        abstained=False,
        abstention_reason=None,
        source="llm",
    )


def compare_proposers(case: CaseData, llm_output: dict | None = None) -> dict[str, ProposerResult]:
    """Side-by-side comparison of rules vs LLM on the same case."""
    return {"rules": rules_propose(case), "llm": llm_propose(case, llm_output)}


def resolve_proposal(case: CaseData, llm_output: dict | None = None) -> ProposerResult:
    """Primary controller entry point: LLM if confident, else rules fallback."""
    llm_result = llm_propose(case, llm_output)
    if not llm_result.abstained:
        return llm_result
    return rules_propose(case)
