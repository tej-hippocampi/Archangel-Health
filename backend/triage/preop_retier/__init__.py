"""
Pre-Op Re-Tier — recompute `Episode.tier` from `initial_tier` + pre-op signals.

Implements the deterministic core of the Pre-Op Re-Tiering PRD v1.0:

  - PAM-style proxy scoring (PRD §4.2)
  - Re-tier hard escalators (PRD §5.2) — short-circuit to TIER_3
  - Soft delta with mutual-exclusion ladders, no double-counting, ±12 cap (§5.3)
  - Delta → tier mapping with sticky-hard guard (§5.4)
  - Idempotent recompute (§5.1) — every call rebuilds from initial + signals

The current public surface is intentionally small — only what the admin
config viewer and unit tests need; signal sourcing, persistence, and the
coordinator UI are out of scope for v1:

    from triage.preop_retier import (
        re_tier_preop,             # main entry point (PRD §5.1)
        score_pam,                 # PAM-13 proxy scorer (PRD §4.2)
        extract_disclosure_flags,  # intake form_data → disclosure codes
        apply_delta_with_guard,    # exposed for boundary tests
        get_config,                # JSON snapshot for the admin endpoint
        MODEL_VERSION,
    )
"""

from triage.preop_retier.algo import re_tier_preop
from triage.preop_retier.intake_disclosures import extract_disclosure_flags
from triage.preop_retier.mapping import apply_delta_with_guard
from triage.preop_retier.pam_proxy import score_pam
from triage.preop_retier.tuning import MODEL_VERSION, get_config

__all__ = [
    "re_tier_preop",
    "score_pam",
    "extract_disclosure_flags",
    "apply_delta_with_guard",
    "get_config",
    "MODEL_VERSION",
]
