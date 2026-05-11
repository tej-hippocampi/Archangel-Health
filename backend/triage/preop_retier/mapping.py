"""
Delta → tier mapping with sticky-hard guard (PRD §5.4).

Asymmetric thresholds: upgrades require less evidence than downgrades
because false-low is dangerous and false-high is just slightly more
labor. Hard-escalator initial tiers are sticky and cannot be downgraded.
"""

from __future__ import annotations

from triage.preop_retier.tuning import DELTA_THRESHOLDS, STICKY_HARD_GUARD
from triage.types import Tier


_TIER_ORDER: tuple[Tier, ...] = ("TIER_1", "TIER_2", "TIER_3")


def upgrade(t: Tier, n: int) -> Tier:
    """Move `n` steps toward TIER_3 (capped at TIER_3)."""
    idx = _TIER_ORDER.index(t)
    return _TIER_ORDER[min(idx + max(n, 0), len(_TIER_ORDER) - 1)]


def downgrade(t: Tier, n: int) -> Tier:
    """Move `n` steps toward TIER_1 (capped at TIER_1)."""
    idx = _TIER_ORDER.index(t)
    return _TIER_ORDER[max(idx - max(n, 0), 0)]


def apply_delta_with_guard(
    initial: Tier,
    initial_was_hard: bool,
    delta: int,
) -> Tier:
    """Map a signed soft delta to a target tier, applying the sticky-hard guard.

    Per PRD §5.4:
      delta ≥ +6  → upgrade 2 steps
      delta ≥ +3  → upgrade 1 step
      delta ≤ -3  → downgrade 1 step (blocked when initial was hard-escalated)
      otherwise   → no change
    """
    if delta >= DELTA_THRESHOLDS["upgrade2_min"]:
        return upgrade(initial, 2)
    if delta >= DELTA_THRESHOLDS["upgrade1_min"]:
        return upgrade(initial, 1)
    if delta <= DELTA_THRESHOLDS["downgrade1_max"]:
        if STICKY_HARD_GUARD and initial_was_hard:
            return initial
        return downgrade(initial, 1)
    return initial
