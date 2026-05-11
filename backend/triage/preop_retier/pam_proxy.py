"""
PAM-style proxy scoring (PRD §4.2).

13 items rated on a 4-point scale (1..4) with optional N/A. Items with
N/A are excluded from scoring; at least 10 non-N/A items are required
for a complete result. Raw average is linearly rescaled into 0..100 and
binned against the published PAM-13 short-form cutoffs.

We do NOT reproduce the licensed PAM-13 instrument — only its scoring
shape and the published cutoffs (PRD §16).
"""

from __future__ import annotations

from triage.preop_retier.tuning import PAM_CUTOFFS
from triage.preop_retier.types import PamLevel, PamResponse, PamResult


_MIN_SCORED_ITEMS = 10


def _level_for(score: float) -> PamLevel:
    """Boundary handling per PRD §13.7:
        score ≤ 55.1  → LOW
        55.1 < score ≤ 67.0  → MODERATE
        score > 67.0  → HIGH
    """
    if score <= PAM_CUTOFFS["low"]:
        return "LOW"
    if score <= PAM_CUTOFFS["moderate"]:
        return "MODERATE"
    return "HIGH"


def score_pam(responses: list[PamResponse]) -> PamResult:
    """Score a set of PAM-style proxy responses. Pure — deterministic
    output for any input set (PRD AC-4.3)."""

    scored = [r for r in responses if r.value != "N_A"]
    items_scored = len(scored)

    if items_scored < _MIN_SCORED_ITEMS:
        # Insufficient — patient must complete more items.
        return PamResult(
            raw_sum=0,
            items_scored=items_scored,
            raw_average=0.0,
            activation_score=0.0,
            level="LOW",
            is_complete=False,
        )

    raw_sum = sum(int(r.value) for r in scored)
    raw_average = raw_sum / items_scored
    # Linear rescale of average 1..4 → 0..100.
    activation_score = round(((raw_average - 1) / 3) * 100, 1)
    return PamResult(
        raw_sum=raw_sum,
        items_scored=items_scored,
        raw_average=raw_average,
        activation_score=activation_score,
        level=_level_for(activation_score),
        is_complete=True,
    )
