"""
Post-Op Scoring & Re-Tiering — recompute `Episode.tier` from the
`post_intraop_tier` floor + current post-op signal state (PRD v1.0).

Implements the deterministic core:

  - Hard escalators (PRD §10.2) — any one ⇒ TIER_3
  - Soft positive contributors (PRD §10.3.a) — unsigned sum, capped at 12
  - Engagement-audit flags (PRD §10.3.b) — logged, contribute 0
  - Delta → tier mapping (PRD §10.4) — ≥+3 +1 step, ≥+6 +2 steps
  - Upward-only resolution against `post_intraop_tier`

Wound-photo signals (PRD §8) are intentionally absent in v1.

Persistence, scoring helpers, lock orchestration, and HTTP routing
live in sibling modules under this package.
"""

from triage.postop.algo import re_tier_post_op
from triage.postop.delta import compute_postop_delta
from triage.postop.hard import evaluate_postop_hard_escalators
from triage.postop.mapping import apply_delta_upward_only, resolve_post_op_tier
from triage.postop.tuning import MODEL_VERSION, TUNING_VERSION, get_config

__all__ = [
    "re_tier_post_op",
    "compute_postop_delta",
    "evaluate_postop_hard_escalators",
    "apply_delta_upward_only",
    "resolve_post_op_tier",
    "get_config",
    "MODEL_VERSION",
    "TUNING_VERSION",
]
