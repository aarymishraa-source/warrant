"""Experiment assignment logic for warrant."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from warrant.config import ASSIGNMENT_SALT

# Arm constants used by policy.py
HOLDOUT = "HOLDOUT"
RULES = "RULES"
WARRANT = "WARRANT"


@dataclass
class Assignment:
    case_id: str
    customer_id: str
    arm: str
    carved_out: bool = False
    carve_reason: Optional[str] = None
    assigned_at: str = ""


def assign_experiment(customer_id: str, salt: str = ASSIGNMENT_SALT) -> dict:
    """Assign a customer to experiment arm using deterministic hashing.

    Args:
        customer_id: Unique customer identifier.
        salt: Salt string for hash derivation.

    Returns:
        dict with 'arm' key: "HOLDOUT" (0-19), "RULES" (20-59), "WARRANT" (60-99).
    """
    hash_input = f"{salt}:{customer_id}"
    hash_hex = hashlib.sha256(hash_input.encode()).hexdigest()
    bucket = int(hash_hex, 16) % 100
    if bucket < 20:
        arm = HOLDOUT
    elif bucket < 60:
        arm = RULES
    else:
        arm = WARRANT
    return {"arm": arm}


def check_carveout(case_data: dict) -> Optional[str]:
    """Check if case qualifies for carveout.

    Priority order:
        1. invalid/missing/empty customer_id
        2. opt_out
        3. upi_autopay_cap_reached

    Args:
        case_data: Dict that may contain 'customer_id', 'upi_autopay_cap_reached',
                   'invalid_customer_id', or 'opt_out' keys.

    Returns:
        Carveout reason string if any carveout condition is met, else None.
    """
    customer_id = case_data.get("customer_id")
    if not customer_id or customer_id.strip() == "":
        return "invalid_customer_id"
    if case_data.get("invalid_customer_id"):
        return "invalid_customer_id"
    if case_data.get("opt_out") is True:
        return "opt_out"
    if case_data.get("upi_autopay_cap_reached"):
        return "upi_autopay_cap_reached"
    return None


def evaluate_assignment(case_data: dict) -> Assignment:
    """Evaluate experiment assignment for a case.

    Args:
        case_data: Dict containing case data with optional carveout fields.
            Must include 'case_id' and 'customer_id'.

    Returns:
        Assignment object. If carveout applies, arm="CARVE_OUT" and carved_out=True.
    """
    case_id = case_data.get("case_id", "")
    customer_id = case_data.get("customer_id", "")
    carveout = check_carveout(case_data)
    if carveout:
        return Assignment(
            case_id=case_id,
            customer_id=customer_id,
            arm="CARVE_OUT",
            carved_out=True,
            carve_reason=carveout,
        )
    result = assign_experiment(customer_id, salt=ASSIGNMENT_SALT)
    return Assignment(
        case_id=case_id,
        customer_id=customer_id,
        arm=result["arm"],
        carved_out=False,
        carve_reason=None,
    )
