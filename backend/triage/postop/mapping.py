"""
Delta → tier mapping for post-op re-tier (PRD §10.4).

Symmetric thresholds with pre-op re-tier (≥+3 = +1 step, ≥+6 = +2
steps), but no downgrade arm: post-op is upward-only and the floor
(`post_intraop_tier`) cannot be undercut.
"""

from __future__ import annotations

from triage.postop.tuning import DELTA_THRESHOLDS, step_up
from triage.types import Tier


def apply_delta_upward_only(floor: Tier, delta: int) -> Tier:
    """Map an unsigned positive delta to a tier upgrade.

    `delta >= upgrade_2_min` → upgrade by 2 (capped at TIER_3)
    `delta >= upgrade_1_min` → upgrade by 1
    Otherwise → return floor unchanged.
    """
    d = max(int(delta), 0)
    if d >= int(DELTA_THRESHOLDS["upgrade_2_min"]):
        return step_up(floor, 2)  # type: ignore[return-value]
    if d >= int(DELTA_THRESHOLDS["upgrade_1_min"]):
        return step_up(floor, 1)  # type: ignore[return-value]
    return floor


_TIER_RANK = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3}


def resolve_post_op_tier(*, floor: Tier, current_tier: Tier, target_tier: Tier) -> Tier:
    """Final tier write-out (PRD §10.1):
    - never below the floor (post_intraop_tier)
    - never below the current tier (most-conservative)
    """
    candidates = [floor, current_tier, target_tier]
    return max(candidates, key=lambda t: _TIER_RANK[t])
