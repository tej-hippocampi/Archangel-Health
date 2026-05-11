"""
Unit tests for `evaluate_postop_hard_escalators` (PRD §10.2).

Covers all six hard escalators present in v1 (wound-photo-driven
escalators are intentionally absent — out of scope v1).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.hard import evaluate_postop_hard_escalators  # noqa: E402
from triage.postop.types import PostOpReTierInput  # noqa: E402


def _state(**overrides) -> PostOpReTierInput:
    base = dict(
        patient_id="p1",
        post_intraop_tier="TIER_1",
        current_tier="TIER_1",
    )
    base.update(overrides)
    return PostOpReTierInput(**base)


def test_clean_state_fires_no_escalators():
    assert evaluate_postop_hard_escalators(_state()) == []


def test_self_flag_fires():
    reasons = evaluate_postop_hard_escalators(_state(has_active_self_flag=True))
    codes = [r.code for r in reasons]
    assert "PATIENT_SELF_FLAG_ACTIVE" in codes


def test_new_red_flag_symptom_today_fires():
    reasons = evaluate_postop_hard_escalators(_state(new_red_flag_symptom_today=True))
    codes = [r.code for r in reasons]
    assert "NEW_RED_FLAG_SYMPTOM" in codes


def test_red_flag_from_d7_survey_fires():
    reasons = evaluate_postop_hard_escalators(_state(day7_red_flag=True))
    codes = [r.code for r in reasons]
    assert "NEW_RED_FLAG_SYMPTOM" in codes


def test_lost_contact_tier3_fires():
    reasons = evaluate_postop_hard_escalators(_state(
        current_tier="TIER_3", lost_contact_tier3_24h=True,
    ))
    codes = [r.code for r in reasons]
    assert "LOST_CONTACT_TIER3" in codes


def test_lost_contact_general_fires():
    reasons = evaluate_postop_hard_escalators(_state(lost_contact_general_72h=True))
    codes = [r.code for r in reasons]
    assert "LOST_CONTACT_GENERAL" in codes


def test_d7_red_with_red_flag_fires():
    reasons = evaluate_postop_hard_escalators(_state(
        day7_tier="RED", day7_red_flag=True,
    ))
    codes = [r.code for r in reasons]
    assert "DAY_X_SURVEY_RED_AND_RED_FLAG" in codes


def test_d14_red_with_red_flag_fires():
    reasons = evaluate_postop_hard_escalators(_state(
        day14_tier="RED", day14_red_flag=True,
    ))
    codes = [r.code for r in reasons]
    assert "DAY_X_SURVEY_RED_AND_RED_FLAG" in codes


def test_d7_red_alone_does_not_fire_compounded_hard():
    """The compounded hard requires BOTH RED total AND red-flag chip."""
    reasons = evaluate_postop_hard_escalators(_state(day7_tier="RED"))
    codes = [r.code for r in reasons]
    # Total RED alone is a soft contributor, not a hard escalator.
    # The NEW_RED_FLAG_SYMPTOM hard is also not fired (no chip).
    assert "DAY_X_SURVEY_RED_AND_RED_FLAG" not in codes


def test_multiple_incision_flags_today_fires():
    reasons = evaluate_postop_hard_escalators(_state(multiple_incision_flags_today=True))
    codes = [r.code for r in reasons]
    assert "MULTIPLE_INCISION_FLAGS" in codes


def test_incision_flag_streak_3_fires():
    reasons = evaluate_postop_hard_escalators(_state(incision_flag_streak=3))
    codes = [r.code for r in reasons]
    assert "MULTIPLE_INCISION_FLAGS" in codes


def test_incision_flag_streak_2_does_not_fire():
    reasons = evaluate_postop_hard_escalators(_state(incision_flag_streak=2))
    codes = [r.code for r in reasons]
    assert "MULTIPLE_INCISION_FLAGS" not in codes


def test_multiple_escalators_listed_in_order():
    """When several escalators trip, all are reported (orchestrator
    short-circuits, but the evaluator returns the full list)."""
    reasons = evaluate_postop_hard_escalators(_state(
        has_active_self_flag=True,
        new_red_flag_symptom_today=True,
        lost_contact_general_72h=True,
    ))
    codes = [r.code for r in reasons]
    assert codes == [
        "PATIENT_SELF_FLAG_ACTIVE",
        "NEW_RED_FLAG_SYMPTOM",
        "LOST_CONTACT_GENERAL",
    ]
