"""
Post-op re-tier orchestrator (`re_tier_post_op`) — PRD §10.1.

Pure function:

  Stage 0 — hard escalators (any one ⇒ TIER_3, short-circuit)
  Stage 1 — sum unsigned soft delta (capped)
  Stage 2 — apply delta upward-only against `post_intraop_tier` floor

Returns a `PostOpReTierResult` with the floor, proposed_tier, delta,
and full ordered list of reasons (HARD / POSITIVE / ENGAGEMENT_AUDIT).
"""

from __future__ import annotations

from triage.postop.delta import compute_postop_delta
from triage.postop.hard import evaluate_postop_hard_escalators
from triage.postop.mapping import apply_delta_upward_only, resolve_post_op_tier
from triage.postop.tuning import MODEL_VERSION, TUNING_VERSION
from triage.postop.types import PostOpReTierInput, PostOpReTierResult


def re_tier_post_op(state: PostOpReTierInput) -> PostOpReTierResult:
    """Run the full Stage-0/1/2 pipeline (PRD §10.1).

    The result `proposed_tier` is the algorithm's output before the
    apply layer's resolve-against-current step. The apply layer then
    reconciles `(floor, current_tier, proposed_tier)` via
    `resolve_post_op_tier` to write `Episode.tier` upward-only.
    """
    # Stage 0 — hard escalators.
    hard_reasons = evaluate_postop_hard_escalators(state)
    if hard_reasons:
        return PostOpReTierResult(
            floor=state.post_intraop_tier,
            proposed_tier="TIER_3",
            delta=0,
            delta_capped=False,
            hard_escalator_fired=True,
            reasons=hard_reasons,
            model_version=MODEL_VERSION,
            tuning_version=TUNING_VERSION,
        )

    # Stage 1 — soft delta.
    delta, capped, soft_reasons = compute_postop_delta(state)

    # Stage 2 — map to tier.
    target = apply_delta_upward_only(state.post_intraop_tier, delta)

    # The algorithm itself does not consult `current_tier`; the apply
    # layer is responsible for upward-only resolution. We do enforce
    # the floor here as a defensive check.
    proposed = resolve_post_op_tier(
        floor=state.post_intraop_tier,
        current_tier=state.post_intraop_tier,
        target_tier=target,
    )

    return PostOpReTierResult(
        floor=state.post_intraop_tier,
        proposed_tier=proposed,
        delta=int(delta),
        delta_capped=bool(capped),
        hard_escalator_fired=False,
        reasons=soft_reasons,
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
    )
