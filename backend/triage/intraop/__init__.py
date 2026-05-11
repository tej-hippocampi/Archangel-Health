"""
Intra-Op Reassessment — recompute `Episode.tier` from a locked intra-op form.

Implements the deterministic core of the Intra-Op Reassessment PRD v1.0:

  - Hard upgrades (PRD §5.1) — any one ⇒ TIER_3
  - Soft upgrades (PRD §5.1) — each adds one step; ≥2 aggregate to TIER_3
  - Procedure-family-specific contributors (PRD §5.1)
  - Most-conservative-wins resolution (PRD §5.1) — upward-only by design
  - Conservative default (PRD §5.2 / §7.4) — 1-step bump when no lock at 24h

The public surface is intentionally focused on the algorithm + tuning
snapshot. Persistence, extraction, and orchestration live in sibling
modules under this package.
"""

from triage.intraop.conservative_default import apply_conservative_default
from triage.intraop.delta import compute_intraop_delta
from triage.intraop.resolve import resolve_final_tier
from triage.intraop.tuning import MODEL_VERSION, TUNING_VERSION, get_config

__all__ = [
    "compute_intraop_delta",
    "resolve_final_tier",
    "apply_conservative_default",
    "get_config",
    "MODEL_VERSION",
    "TUNING_VERSION",
]
