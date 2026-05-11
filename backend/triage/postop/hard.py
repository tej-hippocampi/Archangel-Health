"""
Hard-escalator evaluator for post-op re-tier (PRD §10.2).

Six escalators; any one ⇒ TIER_3:

  1. PATIENT_SELF_FLAG_ACTIVE       — any unresolved self-flag
  2. NEW_RED_FLAG_SYMPTOM           — any item-8 chip on the latest
                                      check-in OR any survey Section A
                                      red-flag chip
  3. LOST_CONTACT_TIER3             — Tier 3 patient silent ≥24h
  4. LOST_CONTACT_GENERAL           — any patient silent ≥72h
  5. DAY_X_SURVEY_RED_AND_RED_FLAG  — D7 or D14 RED total + red-flag chip
  6. MULTIPLE_INCISION_FLAGS        — ≥2 chips same submission OR
                                      single chip on 3 consecutive days

Wound-photo-driven hard escalators are intentionally absent (PRD §8
out of scope v1).
"""

from __future__ import annotations

from triage.postop.tuning import HARD_LABELS
from triage.postop.types import PostOpReTierInput, PostOpReTierReason


def evaluate_postop_hard_escalators(state: PostOpReTierInput) -> list[PostOpReTierReason]:
    """Return ordered list of hard-escalator reasons that fire on this
    state. The orchestrator short-circuits on the first reason."""
    out: list[PostOpReTierReason] = []

    if state.has_active_self_flag:
        out.append(_reason("PATIENT_SELF_FLAG_ACTIVE"))

    if state.new_red_flag_symptom_today or state.day7_red_flag or state.day14_red_flag or state.day30_red_flag:
        out.append(_reason("NEW_RED_FLAG_SYMPTOM"))

    if state.lost_contact_tier3_24h:
        out.append(_reason("LOST_CONTACT_TIER3"))

    if state.lost_contact_general_72h:
        out.append(_reason("LOST_CONTACT_GENERAL"))

    if (state.day7_tier == "RED" and state.day7_red_flag) or (
        state.day14_tier == "RED" and state.day14_red_flag
    ):
        out.append(_reason("DAY_X_SURVEY_RED_AND_RED_FLAG"))

    # Multiple incision flags: ≥2 chips same day or single chip on 3+
    # consecutive days.
    if state.multiple_incision_flags_today or state.incision_flag_streak >= 3:
        out.append(_reason("MULTIPLE_INCISION_FLAGS"))

    # Triage Suite Pass 3 §3.3 — tier-3 Care Companion semantic
    # escalation with an unresolved chat:semantic* row forces TIER_3.
    if state.care_companion_red_flag_unresolved:
        out.append(_reason("CARE_COMPANION_RED_FLAG_TIER_3"))

    return out


def _reason(code: str) -> PostOpReTierReason:
    return PostOpReTierReason(
        kind="HARD",
        code=code,
        label=HARD_LABELS.get(code, code),
        weight=0,
    )
