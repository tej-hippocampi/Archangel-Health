"""
Conservative-default helper (PRD §5.2 / §7.4).

When the form is not locked within `CONSERVATIVE_DEFAULT.threshold_hours_after_or_end`
(default 24h) the system applies a 1-step tier upgrade. The audit
trail records the result with reason
`INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT`. Late lock fires a *new*
reassessment; `resolve_final_tier` keeps the higher tier so the
conservative default stays sticky unless the late lock proposes more.
"""

from __future__ import annotations

from triage.intraop.tuning import (
    CONSERVATIVE_DEFAULT,
    MODEL_VERSION,
    TUNING_VERSION,
    step_up,
)
from triage.intraop.types import IntraopDeltaResult, IntraopReason
from triage.types import Tier


def apply_conservative_default(current_tier: Tier) -> IntraopDeltaResult:
    """Build a synthetic delta result that one-step-upgrades the current tier."""
    steps = CONSERVATIVE_DEFAULT["upgrade_steps"]
    proposed = step_up(current_tier, steps)  # type: ignore[assignment]
    threshold = CONSERVATIVE_DEFAULT["threshold_hours_after_or_end"]
    reason = IntraopReason(
        kind="HARD",  # treated as a definitive system-driven upgrade
        code="INTRAOP_FORM_OVERDUE_CONSERVATIVE_DEFAULT",
        label="Intra-op data unavailable; conservative default applied",
        detail=f"Form not locked within {threshold} h of OR end",
    )
    return IntraopDeltaResult(
        proposed_tier=proposed,
        hard_upgrade_applied=False,
        upgrade_steps=steps,
        is_conservative_default=True,
        reasons=[reason],
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
    )
