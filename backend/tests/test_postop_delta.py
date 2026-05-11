"""
Unit tests for `compute_postop_delta` (PRD §10.3).

Covers each positive contributor weight, the rolling-window cap on
CHECKIN_MISSED, the cap clamp at POSTOP_DELTA_CAP, the engagement-audit
zero-contribution rule, and the care-goal-changed suppression of
missed-engagement contributors.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.delta import compute_postop_delta  # noqa: E402
from triage.postop.tuning import (  # noqa: E402
    CHECKIN_MISSED_ROLLING_CAP_POINTS,
    POSTOP_DELTA_CAP,
    POSTOP_POSITIVE_WEIGHTS,
)
from triage.postop.types import PostOpReTierInput  # noqa: E402


def _state(**overrides) -> PostOpReTierInput:
    base = dict(
        patient_id="p1",
        post_intraop_tier="TIER_1",
        current_tier="TIER_1",
    )
    base.update(overrides)
    return PostOpReTierInput(**base)


def test_clean_state_zero_delta():
    delta, capped, reasons = compute_postop_delta(_state())
    assert delta == 0
    assert capped is False
    assert reasons == []


def test_checkin_red_adds_3():
    delta, _, reasons = compute_postop_delta(_state(last_checkin_tier="RED"))
    assert delta == POSTOP_POSITIVE_WEIGHTS["CHECKIN_TIER_RED"]
    assert any(r.code == "CHECKIN_TIER_RED" for r in reasons)


def test_checkin_orange_adds_1():
    delta, _, _ = compute_postop_delta(_state(last_checkin_tier="ORANGE"))
    assert delta == POSTOP_POSITIVE_WEIGHTS["CHECKIN_TIER_ORANGE"]


def test_checkin_missed_caps_at_5():
    """Five missed days × +1 each = +5; six missed days still capped at +5."""
    d5, _, _ = compute_postop_delta(_state(checkin_missed_count_7d=5))
    d6, _, _ = compute_postop_delta(_state(checkin_missed_count_7d=6))
    assert d5 == CHECKIN_MISSED_ROLLING_CAP_POINTS
    assert d6 == CHECKIN_MISSED_ROLLING_CAP_POINTS


def test_checkin_missed_streak_3_adds_2_extra():
    delta, _, reasons = compute_postop_delta(_state(
        checkin_missed_count_7d=3,
        checkin_missed_streak=3,
    ))
    assert delta == 3 + POSTOP_POSITIVE_WEIGHTS["CHECKIN_MISSED_STREAK_3"]
    assert any(r.code == "CHECKIN_MISSED_STREAK_3" for r in reasons)


def test_wound_concern_from_checkin_adds_2():
    delta, _, _ = compute_postop_delta(_state(wound_concern_today=True))
    assert delta == POSTOP_POSITIVE_WEIGHTS["WOUND_CONCERN_FROM_CHECKIN"]


def test_pain_trajectory_worse_adds_1():
    delta, _, _ = compute_postop_delta(_state(pain_trajectory_abnormal=True))
    assert delta == POSTOP_POSITIVE_WEIGHTS["PAIN_TRAJECTORY_WORSE"]


def test_d7_red_adds_3():
    delta, _, _ = compute_postop_delta(_state(day7_tier="RED"))
    assert delta == POSTOP_POSITIVE_WEIGHTS["SURVEY_DAY_7_RED"]


def test_d14_red_and_d30_orange():
    delta, _, _ = compute_postop_delta(_state(day14_tier="RED", day30_tier="ORANGE"))
    assert delta == (
        POSTOP_POSITIVE_WEIGHTS["SURVEY_DAY_14_RED"]
        + POSTOP_POSITIVE_WEIGHTS["SURVEY_DAY_30_ORANGE"]
    )


def test_d7_missed_and_d14_missed():
    delta, _, _ = compute_postop_delta(_state(day7_missed=True, day14_missed=True))
    assert delta == (
        POSTOP_POSITIVE_WEIGHTS["SURVEY_DAY_7_MISSED"]
        + POSTOP_POSITIVE_WEIGHTS["SURVEY_DAY_14_MISSED"]
    )


def test_red_flag_video_not_viewed_by_d5_only_after_threshold():
    """Pre-threshold: doesn't fire. Post-threshold: fires."""
    pre = _state(days_since_discharge=4)
    post = _state(days_since_discharge=6)
    delta_pre, _, _ = compute_postop_delta(pre)
    delta_post, _, reasons = compute_postop_delta(post)
    assert delta_pre == 0
    assert delta_post == POSTOP_POSITIVE_WEIGHTS["RED_FLAG_VIDEO_NOT_VIEWED_BY_D5"]
    assert any(r.code == "RED_FLAG_VIDEO_NOT_VIEWED_BY_D5" for r in reasons)


def test_red_flag_video_viewed_by_d2_is_audit_only():
    """Engagement-reward flag fires as audit reason but contributes 0."""
    delta, _, reasons = compute_postop_delta(_state(
        red_flag_video_viewed_by_d2=True, red_flag_video_viewed_by_d5=True,
        days_since_discharge=6,
    ))
    assert delta == 0
    audit = [r for r in reasons if r.kind == "ENGAGEMENT_AUDIT"]
    assert any(r.code == "RED_FLAG_VIDEO_VIEWED_BY_D2" for r in audit)


def test_med_adherence_low_adds_2():
    delta, _, _ = compute_postop_delta(_state(med_adherence_low=True))
    assert delta == POSTOP_POSITIVE_WEIGHTS["MED_ADHERENCE_LOW"]


def test_med_adherence_high_audit_only():
    delta, _, reasons = compute_postop_delta(_state(med_adherence_high=True))
    assert delta == 0
    assert any(r.code == "MED_ADHERENCE_HIGH" and r.kind == "ENGAGEMENT_AUDIT" for r in reasons)


def test_non_response_streak_adds_2():
    delta, _, _ = compute_postop_delta(_state(med_adherence_non_response_streak_3=True))
    assert delta == POSTOP_POSITIVE_WEIGHTS["MED_ADHERENCE_NON_RESPONSE_STREAK_3"]


def test_delta_cap_clamps_at_12():
    """Stack many contributors to exceed the cap."""
    delta, capped, _ = compute_postop_delta(_state(
        last_checkin_tier="RED",                # +3
        checkin_missed_count_7d=5,              # +5
        checkin_missed_streak=3,                # +2
        wound_concern_today=True,               # +2
        day7_tier="RED",                        # +3
        day14_tier="RED",                       # +3
        med_adherence_low=True,                 # +2
        med_adherence_non_response_streak_3=True,  # +2
        days_since_discharge=20,
    ))
    assert delta == POSTOP_DELTA_CAP
    assert capped is True


def test_care_goal_change_suppresses_missed_engagement():
    """Care-goal-changed suppresses missed-engagement contributors but
    not safety contributors (PRD §17.7)."""
    base_state = _state(
        wound_concern_today=True,                  # safety; preserved
        day7_missed=True,                          # missed; suppressed
        med_adherence_non_response_streak_3=True,  # missed; suppressed
        checkin_missed_count_7d=4,                 # missed; suppressed
        checkin_missed_streak=4,                   # missed; suppressed
    )
    no_pivot = compute_postop_delta(base_state)
    pivot = compute_postop_delta(base_state.model_copy(update={"care_goal_changed": True}))
    assert no_pivot[0] > pivot[0]
    # WOUND_CONCERN_FROM_CHECKIN is a safety signal — preserved through the pivot.
    assert pivot[0] == POSTOP_POSITIVE_WEIGHTS["WOUND_CONCERN_FROM_CHECKIN"]
