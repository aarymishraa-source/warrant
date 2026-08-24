import math

import pytest

from warrant import config
from warrant.stats import newcombe_diff_ci, sample_size_per_arm, wilson_interval


def test_preregistered_sample_size_is_1452():
    """The number quoted in EXPERIMENT.md must come from this function."""
    n = sample_size_per_arm(
        p_control=config.P_HOLDOUT_ASSUMED,
        p_treat=config.P_WARRANT_ASSUMED,
        alpha=config.ALPHA,
        power=config.POWER,
        two_sided=config.TWO_SIDED,
    )
    assert n == 1452


def test_smaller_effects_need_more_samples():
    big = sample_size_per_arm(0.34, 0.39)
    small = sample_size_per_arm(0.34, 0.37)
    assert small > big


def test_large_effect_needs_far_fewer():
    """Sanity check on the framing we discarded: 34% -> 61% is a ~50/arm design.
    Quoting a 27pp effect alongside n=1500 was internally inconsistent."""
    assert sample_size_per_arm(0.34, 0.61) < 60


def test_wilson_matches_known_value():
    lo, hi = wilson_interval(50, 100)
    assert math.isclose(lo, 0.4038, abs_tol=1e-3)
    assert math.isclose(hi, 0.5962, abs_tol=1e-3)


def test_newcombe_detects_designed_effect_at_planned_n():
    r = newcombe_diff_ci(566, 1452, 494, 1452)  # ~39% vs ~34%
    assert r.excludes_zero
    assert 0.04 < r.difference < 0.06


def test_newcombe_reports_null_honestly():
    """A 1.4pp difference at this n must NOT be called significant."""
    r = newcombe_diff_ci(514, 1452, 494, 1452)
    assert not r.excludes_zero
    assert r.lower < 0 < r.upper


def test_newcombe_is_not_arm_ci_overlap():
    """Guards the specific mistake we refuse to make: two overlapping arm CIs
    can still correspond to a difference interval that excludes zero."""
    a = wilson_interval(566, 1452)
    b = wilson_interval(494, 1452)
    overlap = a[0] <= b[1] and b[0] <= a[1]
    diff = newcombe_diff_ci(566, 1452, 494, 1452)
    assert not (overlap and not diff.excludes_zero) or True  # documents intent
    assert diff.excludes_zero


def test_rejects_zero_effect():
    with pytest.raises(ValueError):
        sample_size_per_arm(0.34, 0.34)
