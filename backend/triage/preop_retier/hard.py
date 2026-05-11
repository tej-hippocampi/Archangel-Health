"""
Re-tier hard escalators (PRD §5.2).

If any condition fires, the algorithm short-circuits to TIER_3 regardless
of `initial_tier` or the soft delta. The first fired condition is
recorded as the sole reason in the result.
"""

from __future__ import annotations

from typing import Optional

from triage.preop_retier.tuning import HARD_LABELS
from triage.preop_retier.types import PreOpReTierInput, ReTierReason


_INTAKE_DISCLOSURE_CODES = (
    "INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER",
    "INTAKE_DISCLOSURE_HOUSING_INSTABILITY",
    "INTAKE_DISCLOSURE_FOOD_INSECURITY",
    "INTAKE_DISCLOSURE_TRANSPORTATION_BARRIER_DAY_OF",
)


def _hard_reason(code: str) -> ReTierReason:
    return ReTierReason(kind="HARD", code=code, label=HARD_LABELS.get(code, code))


def evaluate_hard_escalators(state: PreOpReTierInput) -> Optional[ReTierReason]:
    """Return the first hard escalator that fires, or None.

    Order is intentional:
      1. Intake disclosures (most actionable / definitively known once intake submitted)
      2. Critical survey red flag (highest acuity — symptom-driven)
      3. PAM LOW at T-24 (cadence-gated; only applies once remediation window closed)
    """
    # 1. Intake disclosures
    disclosed = set(state.intake.disclosures or [])
    for code in _INTAKE_DISCLOSURE_CODES:
        if code in disclosed:
            return _hard_reason(code)

    # 2. Critical red survey flag — only T-48 / T-24 windows count (PRD §5.2).
    for s in state.surveys:
        if s.window in ("T_48", "T_24") and s.status == "RED" and s.has_critical_red_flag:
            return _hard_reason("SURVEY_RED_FLAG_CRITICAL")

    # 3. PAM LOW at T-24 (no remediation window remaining).
    if (
        state.pam is not None
        and state.pam.is_complete
        and state.pam.level == "LOW"
        and state.hours_until_surgery <= 24
    ):
        return _hard_reason("PAM_LEVEL_LOW_AT_T_24")

    return None
