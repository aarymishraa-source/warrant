"""
The only statistics Warrant needs. Deliberately small.

Two functions:
  sample_size_per_arm  - how many cases per arm the design requires
  newcombe_diff_ci     - interval for the DIFFERENCE of two proportions

Why Newcombe and not "do the two arm CIs overlap":
overlap of two separate intervals is not a hypothesis test for the difference.
The estimand is p_treat - p_control, so the interval must be built on that
quantity directly. Newcombe (1998) Method 10 composes two Wilson score
intervals and has good coverage at small n and at proportions near 0 or 1.

No scipy. z-values for the handful of levels we use are hardcoded and tested.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Standard normal quantiles. Hardcoded to avoid a scipy dependency for 3 numbers.
_Z = {
    0.80: 0.8416212335729143,   # z for power 0.80 (one-sided beta)
    0.90: 1.2815515655446004,
    0.95: 1.6448536269514722,   # one-sided alpha 0.05
    0.975: 1.959963984540054,   # two-sided alpha 0.05
    0.995: 2.5758293035489004,
}


def z(p: float) -> float:
    """Standard normal quantile for a supported level."""
    if p not in _Z:
        raise ValueError(f"unsupported quantile {p}; supported: {sorted(_Z)}")
    return _Z[p]


def sample_size_per_arm(
    p_control: float,
    p_treat: float,
    alpha: float = 0.05,
    power: float = 0.80,
    two_sided: bool = True,
) -> int:
    """
    Cases per arm for a two-proportion comparison.

        n = (z_{alpha/2} + z_beta)^2 * [p1(1-p1) + p2(1-p2)] / (p1 - p2)^2

    Returns the ceiling. This is a DESIGN quantity, not a measurement.
    """
    if not (0 < p_control < 1 and 0 < p_treat < 1):
        raise ValueError("proportions must be strictly between 0 and 1")
    delta = abs(p_treat - p_control)
    if delta == 0:
        raise ValueError("effect size must be non-zero")

    z_alpha = z(1 - alpha / 2) if two_sided else z(1 - alpha)
    z_beta = z(power)
    variance = p_control * (1 - p_control) + p_treat * (1 - p_treat)
    return math.ceil((z_alpha + z_beta) ** 2 * variance / delta**2)


def wilson_interval(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a single proportion."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be within [0, n]")

    zq = z(1 - alpha / 2)
    p = successes / n
    denom = 1 + zq**2 / n
    centre = (p + zq**2 / (2 * n)) / denom
    spread = zq * math.sqrt(p * (1 - p) / n + zq**2 / (4 * n**2)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


@dataclass(frozen=True)
class DiffResult:
    p_treat: float
    p_control: float
    difference: float
    lower: float
    upper: float
    excludes_zero: bool

    def __str__(self) -> str:  # pragma: no cover - display only
        return (
            f"{self.p_treat:.1%} - {self.p_control:.1%} = "
            f"{self.difference:+.1%}  95% CI [{self.lower:+.1%}, {self.upper:+.1%}]"
            f"  {'significant' if self.excludes_zero else 'CI CROSSES ZERO'}"
        )


def newcombe_diff_ci(
    successes_treat: int,
    n_treat: int,
    successes_control: int,
    n_control: int,
    alpha: float = 0.05,
) -> DiffResult:
    """
    Newcombe Method 10 interval for p_treat - p_control.

    Composes the two Wilson intervals rather than using a normal approximation
    on the difference, which behaves badly at small n.
    """
    p1 = successes_treat / n_treat
    p2 = successes_control / n_control
    l1, u1 = wilson_interval(successes_treat, n_treat, alpha)
    l2, u2 = wilson_interval(successes_control, n_control, alpha)

    diff = p1 - p2
    lower = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    lower, upper = max(-1.0, lower), min(1.0, upper)

    return DiffResult(
        p_treat=p1,
        p_control=p2,
        difference=diff,
        lower=lower,
        upper=upper,
        excludes_zero=(lower > 0 or upper < 0),
    )
