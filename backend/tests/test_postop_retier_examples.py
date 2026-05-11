"""
Worked examples from PRD §10.5 (Examples A–G).

These encode the seven canonical scenarios the PRD names. Wound-photo-
specific fixtures are intentionally absent (PRD §8 out of scope v1) — the
examples that referenced wound-photo are reframed against signals
preserved in v1 (the daily check-in's incision-flag chip, MULTIPLE_INCISION_FLAGS).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.algo import re_tier_post_op  # noqa: E402
from triage.postop.types import PostOpReTierInput  # noqa: E402


def _state(**overrides) -> PostOpReTierInput:
    base = dict(
        patient_id="p1",
        post_intraop_tier="TIER_1",
        current_tier="TIER_1",
    )
    base.update(overrides)
    return PostOpReTierInput(**base)


# ─── Example A — Floor TIER_1, clean recovery ────────────────────────────────


def test_example_a_clean_recovery_stays_t1():
    """Engagement-audit flags fire (logged) but contribute 0; delta = 0; tier stays TIER_1."""
    res = re_tier_post_op(_state(
        red_flag_video_viewed_by_d2=True,
        red_flag_video_viewed_by_d5=True,
        diag_treat_video_viewed_by_d5=True,
        diag_treat_video_sessions_total=3,
        diag_treat_video_viewed_by_d14=True,
        med_adherence_high=True,
        days_since_discharge=14,
    ))
    assert res.proposed_tier == "TIER_1"
    assert res.delta == 0
    assert res.hard_escalator_fired is False
    audit_codes = [r.code for r in res.reasons if r.kind == "ENGAGEMENT_AUDIT"]
    assert "RED_FLAG_VIDEO_VIEWED_BY_D2" in audit_codes
    assert "MED_ADHERENCE_HIGH" in audit_codes


# ─── Example B — Floor TIER_1, missed engagement ────────────────────────────


def test_example_b_missed_engagement_pushes_to_t3():
    """D7 missed (+2) + D14 orange (+1) + 4 missed check-ins (+4) +
    red-flag video not viewed by D5 (+2) + (no wound-photo in v1) → ≥+6 → +2 steps → TIER_3."""
    res = re_tier_post_op(_state(
        day7_missed=True,
        day14_tier="ORANGE",
        checkin_missed_count_7d=4,
        red_flag_video_viewed_by_d5=False,
        days_since_discharge=14,
    ))
    assert res.delta >= 6
    assert res.proposed_tier == "TIER_3"


# ─── Example C — Floor TIER_2, single incision-flag chip ─────────────────────


def test_example_c_floor_t2_single_chip_stays_t2():
    """+2 from WOUND_CONCERN_FROM_CHECKIN; <+3 → no change; tier stays TIER_2.
    (The wound concern still raises an alert via the apply layer; the
    algorithm itself does not move the tier.)"""
    res = re_tier_post_op(_state(
        post_intraop_tier="TIER_2",
        current_tier="TIER_2",
        wound_concern_today=True,
    ))
    assert res.delta == 2
    assert res.proposed_tier == "TIER_2"
    assert res.hard_escalator_fired is False


# ─── Example D — Hard escalator from compounded item-5 ──────────────────────


def test_example_d_incision_flag_streak_3_consecutive_hard_t3():
    """Three consecutive daily check-ins each show "new redness spreading."
    Hard escalator MULTIPLE_INCISION_FLAGS → TIER_3 regardless of soft delta."""
    res = re_tier_post_op(_state(incision_flag_streak=3))
    assert res.proposed_tier == "TIER_3"
    assert res.hard_escalator_fired is True
    codes = [r.code for r in res.reasons]
    assert "MULTIPLE_INCISION_FLAGS" in codes


# ─── Example E — Floor TIER_3, bad recovery ─────────────────────────────────


def test_example_e_floor_t3_stays_t3():
    """Already at T3; soft delta cannot push higher; hard escalators may
    fire but tier still T3."""
    res = re_tier_post_op(_state(
        post_intraop_tier="TIER_3",
        current_tier="TIER_3",
        last_checkin_tier="RED",
        day7_tier="RED",
    ))
    assert res.proposed_tier == "TIER_3"


# ─── Example F — Floor TIER_1, lost contact 72h ─────────────────────────────


def test_example_f_lost_contact_general_72h_hard_t3():
    res = re_tier_post_op(_state(lost_contact_general_72h=True))
    assert res.proposed_tier == "TIER_3"
    assert res.hard_escalator_fired is True
    codes = [r.code for r in res.reasons]
    assert "LOST_CONTACT_GENERAL" in codes


# ─── Example G — Floor TIER_1, self-flag at D6 ──────────────────────────────


def test_example_g_self_flag_active_hard_t3():
    """Active self-flag → TIER_3 hard. Algorithm itself never downgrades;
    RN action with reason is the only post-op downgrade path."""
    res = re_tier_post_op(_state(has_active_self_flag=True))
    assert res.proposed_tier == "TIER_3"
    assert res.hard_escalator_fired is True
    codes = [r.code for r in res.reasons]
    assert "PATIENT_SELF_FLAG_ACTIVE" in codes
