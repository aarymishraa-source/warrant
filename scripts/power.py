"""Reproduce every number quoted in EXPERIMENT.md. Run: python scripts/power.py"""
from warrant import config
from warrant.stats import newcombe_diff_ci, sample_size_per_arm

n = sample_size_per_arm(
    config.P_HOLDOUT_ASSUMED, config.P_WARRANT_ASSUMED,
    config.ALPHA, config.POWER, config.TWO_SIDED,
)
print(f"seed                : {config.SEED}")
print(f"design              : {config.P_HOLDOUT_ASSUMED:.0%} -> {config.P_WARRANT_ASSUMED:.0%}"
      f"  (+{(config.P_WARRANT_ASSUMED - config.P_HOLDOUT_ASSUMED)*100:.0f}pp)")
print(f"alpha / power       : {config.ALPHA} two-sided / {config.POWER}")
print(f"n per arm           : {n}")
print()
print("worked examples (SYNTHETIC - illustrating the estimator, not results):")
print(f"  designed effect   : {newcombe_diff_ci(566, n, 494, n)}")
print(f"  null result       : {newcombe_diff_ci(514, n, 494, n)}")
print()
print(f"discarded framing   : 34% -> 61% requires "
      f"{sample_size_per_arm(0.34, 0.61)} per arm, not 1500. Permanently removed.")
