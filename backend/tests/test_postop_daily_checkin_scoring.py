"""
Unit tests for `score_daily_checkin` (PRD §4.2).

Covers every per-item scoring cell, the GREEN/ORANGE/RED tier thresholds,
the item-5 / item-8 event passthrough, and the `is_pain_above_expected_curve`
helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.scoring.daily_checkin import (  # noqa: E402
    is_pain_above_expected_curve,
    score_daily_checkin,
)
from triage.postop.types import DailyCheckinAnswers  # noqa: E402


def _answers(**overrides) -> DailyCheckinAnswers:
    base = dict(
        pain_nrs=2,
        pain_trajectory="BETTER",
        fever="NO",
        incision_change="BETTER",
        incision_flags=[],
        nausea="NONE",
        eating_drinking="YES",
        red_flag_symptoms=[],
        walking="YES",
        worry_level="NOT_AT_ALL",
    )
    base.update(overrides)
    return DailyCheckinAnswers(**base)


# ─── Per-item weight matrix ─────────────────────────────────────────────────


def test_pain_nrs_extremes():
    s_low = score_daily_checkin(_answers(pain_nrs=0)).item_scores["pain_nrs"]
    s_high = score_daily_checkin(_answers(pain_nrs=10)).item_scores["pain_nrs"]
    assert s_low == 100.0
    assert s_high == 0.0


def test_pain_trajectory_mapping():
    assert score_daily_checkin(_answers(pain_trajectory="BETTER")).item_scores["pain_trajectory"] == 100.0
    assert score_daily_checkin(_answers(pain_trajectory="SAME")).item_scores["pain_trajectory"] == 70.0
    assert score_daily_checkin(_answers(pain_trajectory="WORSE")).item_scores["pain_trajectory"] == 20.0


def test_fever_mapping():
    assert score_daily_checkin(_answers(fever="NO")).item_scores["fever"] == 100.0
    assert score_daily_checkin(_answers(fever="YES_FELT")).item_scores["fever"] == 40.0
    assert score_daily_checkin(_answers(fever="YES_MEASURED")).item_scores["fever"] == 0.0


def test_incision_change_mapping():
    assert score_daily_checkin(_answers(incision_change="BETTER")).item_scores["incision_change"] == 100.0
    assert score_daily_checkin(_answers(incision_change="SAME")).item_scores["incision_change"] == 85.0
    assert score_daily_checkin(_answers(incision_change="WORSE")).item_scores["incision_change"] == 10.0


def test_incision_flags_single_vs_multiple():
    a = score_daily_checkin(_answers(incision_flags=["NEW_REDNESS_SPREADING"]))
    b = score_daily_checkin(_answers(incision_flags=["NEW_REDNESS_SPREADING", "NEW_DRAINAGE"]))
    assert a.item_scores["incision_flags"] == 20.0
    assert b.item_scores["incision_flags"] == 0.0
    assert a.wound_concern is True
    assert b.wound_concern is True


def test_nausea_mapping():
    for value, expected in (("NONE", 100.0), ("MILD", 70.0), ("MODERATE", 40.0), ("SEVERE", 10.0)):
        assert score_daily_checkin(_answers(nausea=value)).item_scores["nausea"] == expected


def test_eating_mapping():
    for value, expected in (("YES", 100.0), ("SOME", 60.0), ("ALMOST_NOTHING", 20.0)):
        assert score_daily_checkin(_answers(eating_drinking=value)).item_scores["eating_drinking"] == expected


def test_red_flag_symptoms_drops_score_to_zero():
    a = score_daily_checkin(_answers(red_flag_symptoms=["CHEST_PAIN"]))
    assert a.item_scores["red_flag_symptoms"] == 0.0
    assert a.new_red_flag_symptom is True


def test_walking_mapping():
    for value, expected in (("YES", 100.0), ("SOME", 60.0), ("NO", 20.0)):
        assert score_daily_checkin(_answers(walking=value)).item_scores["walking"] == expected


def test_worry_mapping():
    for value, expected in (
        ("NOT_AT_ALL", 100.0), ("A_LITTLE", 80.0), ("MODERATELY", 50.0),
        ("VERY", 20.0), ("EXTREMELY", 0.0),
    ):
        assert score_daily_checkin(_answers(worry_level=value)).item_scores["worry_level"] == expected


# ─── Tier mapping ──────────────────────────────────────────────────────────


def test_clean_checkin_is_green():
    res = score_daily_checkin(_answers())
    assert res.tier == "GREEN"
    assert res.raw_total >= 95
    assert res.new_red_flag_symptom is False
    assert res.wound_concern is False


def test_red_flag_chip_forces_red_regardless_of_total():
    res = score_daily_checkin(_answers(red_flag_symptoms=["CHEST_PAIN"]))
    assert res.tier == "RED"
    assert res.new_red_flag_symptom is True
    assert "CHEST_PAIN" in res.red_flags


def test_multiple_incision_flags_force_red():
    res = score_daily_checkin(_answers(
        incision_flags=["NEW_REDNESS_SPREADING", "NEW_DRAINAGE"],
    ))
    assert res.tier == "RED"
    assert res.wound_concern is True


def test_single_incision_flag_is_orange_when_total_is_high():
    """PRD §4.2: any single item-5 chip without item-8 → ORANGE alert at OPEN."""
    res = score_daily_checkin(_answers(incision_flags=["BAD_SMELL"]))
    assert res.tier == "ORANGE"
    assert res.wound_concern is True


def test_low_total_drops_to_red():
    """Multiple low-scoring items push raw_total below 70."""
    res = score_daily_checkin(_answers(
        pain_nrs=8,
        pain_trajectory="WORSE",
        fever="YES_MEASURED",
        nausea="SEVERE",
        eating_drinking="ALMOST_NOTHING",
        worry_level="EXTREMELY",
    ))
    assert res.raw_total < 70
    assert res.tier == "RED"


# ─── Pain expected curve ────────────────────────────────────────────────────


def test_pain_above_expected_curve_early_episode():
    # Day 1 floor is 8; NRS 9 is above curve.
    assert is_pain_above_expected_curve(episode_day=1, pain_nrs=9) is True
    assert is_pain_above_expected_curve(episode_day=1, pain_nrs=7) is False


def test_pain_above_expected_curve_late_episode():
    # Day 30 floor is 2; NRS 4 is above curve.
    assert is_pain_above_expected_curve(episode_day=30, pain_nrs=4) is True
    assert is_pain_above_expected_curve(episode_day=30, pain_nrs=2) is False


def test_pain_above_expected_curve_beyond_table():
    # Beyond the table we re-use the last known floor (day 30 = 2).
    assert is_pain_above_expected_curve(episode_day=999, pain_nrs=3) is True
    assert is_pain_above_expected_curve(episode_day=999, pain_nrs=1) is False
