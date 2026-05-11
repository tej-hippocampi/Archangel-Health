"""
Final-tier resolution (PRD §5.1).

Upward-only: the intra-op pass never downgrades. A pre-op TIER_3
patient with an uneventful surgery stays TIER_3. A pre-op TIER_1
patient with a hard intra-op event is upgraded to TIER_3.
"""

from __future__ import annotations

from triage.types import Tier


_TIER_RANK: dict[str, int] = {"TIER_1": 1, "TIER_2": 2, "TIER_3": 3}


def resolve_final_tier(current_tier: Tier, intraop_proposed_tier: Tier) -> Tier:
    """Return the higher-rank of (current, proposed). Most conservative wins."""
    if _TIER_RANK[current_tier] >= _TIER_RANK[intraop_proposed_tier]:
        return current_tier
    return intraop_proposed_tier
