"""
Triage — pre-op risk tiering for TEAM-eligible episodes.

This package implements the deterministic core of the Initial Pre-Op Triage
algorithm (PRD: Initial Pre-Op Triage v1.0):

  1. Hard escalators short-circuit to TIER_3 (e.g. EMERGENCY_CASE,
     SEPSIS_48H, FUNCTIONAL_TOTALLY_DEPENDENT, LIVES_ALONE_NO_CAREGIVER, ...)
  2. Otherwise sum a procedure-family base risk + weighted soft factors and
     map the total to TIER_1 / TIER_2 / TIER_3.

The current public surface is intentionally small — only what the admin
config viewer and unit tests need:

    from triage import (
        assign_initial_tier,    # main entry point (PRD §5.5)
        score_to_tier,          # exposed for boundary tests
        get_config,             # JSON snapshot for the admin endpoint
        MODEL_VERSION,
    )
"""

from triage.initial_tier import assign_initial_tier, score_to_tier
from triage.tuning import MODEL_VERSION, get_config

__all__ = [
    "assign_initial_tier",
    "score_to_tier",
    "get_config",
    "MODEL_VERSION",
]
