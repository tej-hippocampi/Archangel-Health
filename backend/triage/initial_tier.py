"""
Initial Pre-Op Triage — main algorithm (PRD §5.5).

Two-stage decision:

  Stage 1: any hard escalator → TIER_3, score = None, single reason logged.
  Stage 2: PROCEDURE_BASE[family] + Σ soft-factor weights → score → tier.
"""

from __future__ import annotations

from triage.derive_flags import derive_flags
from triage.tuning import (
    HARD_LABELS,
    MODEL_VERSION,
    PROCEDURE_BASE,
    PROCEDURE_LABELS,
    SCORE_TO_TIER,
    SOFT_LABELS,
    TUNING_VERSION,
)
from triage.types import InitialTierInput, Tier, TierAssignment, TierReason


def score_to_tier(score: int) -> Tier:
    """Map a soft score to a tier per PRD §5.3 and `tuning.SCORE_TO_TIER`."""
    if score >= SCORE_TO_TIER["tier3_min"]:
        return "TIER_3"
    if score >= SCORE_TO_TIER["tier2_min"]:
        return "TIER_2"
    return "TIER_1"


def assign_initial_tier(input: InitialTierInput) -> TierAssignment:
    """Assign the initial pre-op tier for a TEAM-eligible episode.

    See PRD §5.5 for the deterministic two-stage decision.
    """
    flags = derive_flags(input)

    # ─── Stage 1 — hard escalators ─────────────────────────────────────────
    if flags["hard"]:
        first = flags["hard"][0]
        return TierAssignment(
            tier="TIER_3",
            score=None,
            reasons=[TierReason(
                kind="HARD",
                code=first,
                label=HARD_LABELS.get(first, first),
            )],
            model_version=MODEL_VERSION,
            tuning_version=TUNING_VERSION,
        )

    # ─── Stage 2 — weighted soft score ─────────────────────────────────────
    family = input.procedure.anchor_procedure_family
    base = PROCEDURE_BASE.get(family, 0)

    reasons: list[TierReason] = [TierReason(
        kind="BASE",
        code="PROCEDURE_BASE",
        label=f"{PROCEDURE_LABELS.get(family, family)} base risk",
        weight=base,
    )]

    score = base
    for flag, weight in flags["soft"]:
        score += weight
        reasons.append(TierReason(
            kind="SOFT",
            code=flag,
            label=SOFT_LABELS.get(flag, flag),
            weight=weight,
        ))

    return TierAssignment(
        tier=score_to_tier(score),
        score=score,
        reasons=reasons,
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
    )
