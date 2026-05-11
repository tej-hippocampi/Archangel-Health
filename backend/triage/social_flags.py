"""
Social / SDoH-derived flags.

The PRD treats three social factors as hard escalators (housing instability,
food insecurity, lives-alone-without-caregiver) and the rest as soft.
"""

from __future__ import annotations

from triage.types import SocialHistoryInput


def derive_social_flags(social: SocialHistoryInput) -> set[str]:
    flags: set[str] = set()

    # ─── Hard escalators (PRD §5.1) ────────────────────────────────────────
    if social.housing_status in ("HOMELESS", "UNSTABLE"):
        flags.add("HOUSING_INSTABILITY")

    if social.food_security == "INSECURE":
        flags.add("FOOD_INSECURITY")

    if social.lives_alone is True and social.has_reliable_caregiver is False:
        flags.add("LIVES_ALONE_NO_CAREGIVER")

    # ─── Soft factors ──────────────────────────────────────────────────────
    if social.smoking_status == "CURRENT":
        flags.add("CURRENT_SMOKER")
        if social.pack_years is not None and social.pack_years > 20:
            flags.add("CURRENT_SMOKER_HEAVY")

    if social.alcohol_use in ("HEAVY", "AT_RISK_OR_AUDIT_POSITIVE"):
        flags.add("AT_RISK_ALCOHOL_OR_AUDIT_POS")

    if any(s.status == "ACTIVE" for s in social.substance_use):
        flags.add("ACTIVE_SUBSTANCE_USE")

    if social.age >= 75:
        flags.add("AGE_75_PLUS")

    if social.transportation_barrier is True:
        flags.add("TRANSPORTATION_BARRIER")

    if social.needs_interpreter is True:
        flags.add("NEEDS_INTERPRETER")

    return flags
