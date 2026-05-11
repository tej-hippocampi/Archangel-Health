"""
Main entry point for Pre-Op Re-Tier (PRD §5.1).

`re_tier_preop` is idempotent — every call rebuilds the tier from
`initial_tier` + the current state of all five signal sources. There is
no event-by-event mutation. Hard escalators short-circuit; otherwise we
sum a signed soft delta and apply it to `initial_tier` with the
sticky-hard guard.
"""

from __future__ import annotations

from triage.preop_retier.delta import compute_preop_delta
from triage.preop_retier.hard import evaluate_hard_escalators
from triage.preop_retier.mapping import apply_delta_with_guard
from triage.preop_retier.tuning import MODEL_VERSION, TUNING_VERSION
from triage.preop_retier.types import PreOpReTierInput, PreOpReTierResult


def re_tier_preop(state: PreOpReTierInput) -> PreOpReTierResult:
    """Recompute the live tier from `initial_tier` + signal state.

    Stages (PRD §5.1):
      0. Re-tier hard escalators → if any fires, TIER_3 with single reason.
      1. Soft-delta computation across the five signal categories.
      2. Apply delta with sticky-hard guard → computed tier.
    """

    # ─── Stage 0 — re-tier hard escalators ─────────────────────────────────
    hard = evaluate_hard_escalators(state)
    if hard is not None:
        return PreOpReTierResult(
            initial_tier=state.initial_tier,
            initial_tier_was_hard=state.initial_tier_was_hard_escalator,
            delta=0,
            soft_cap_applied=False,
            computed_tier="TIER_3",
            reasons=[hard],
            model_version=MODEL_VERSION,
            tuning_version=TUNING_VERSION,
        )

    # ─── Stages 1 & 2 — soft delta + sticky-guarded mapping ────────────────
    delta, reasons, soft_cap_applied = compute_preop_delta(state)
    computed_tier = apply_delta_with_guard(
        state.initial_tier,
        state.initial_tier_was_hard_escalator,
        delta,
    )

    return PreOpReTierResult(
        initial_tier=state.initial_tier,
        initial_tier_was_hard=state.initial_tier_was_hard_escalator,
        delta=delta,
        soft_cap_applied=soft_cap_applied,
        computed_tier=computed_tier,
        reasons=reasons,
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
    )
