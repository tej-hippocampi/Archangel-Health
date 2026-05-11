"""
Unit tests for `score_day_survey` (PRD §5.2).

Covers per-day section weights, the procedure-family Section B mapping,
red-flag passthrough, and the GREEN/ORANGE/RED tier thresholds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.scoring.day_survey import score_day_survey  # noqa: E402
from triage.postop.types import DayXSurveyAnswers  # noqa: E402


def _clean_answers(family: str = "LEJR") -> DayXSurveyAnswers:
    """A patient with quietly perfect answers across all four sections."""
    return DayXSurveyAnswers(
        section_a={
            "pain_nrs": 1,
            "pain_interference": {"work": 1, "sleep": 1, "mood": 1, "enjoyment": 1},
        },
        section_b={
            "stiffness": 95, "pain": 95, "function": 95, "stairs": 95, "rising": 95,
            "general_health": 95, "physical_function": 95, "role_physical": 95,
            "energy": 95, "social_function": 95,
            "pain_intensity": 1, "personal_care": 1, "lifting": 1,
            "walking": 1, "sitting": 1, "standing": 1,
        },
        section_c={
            "remembered_to_take": True, "took_yesterday": True, "stopped_when_better": True,
            "missed_when_traveling": True, "took_today": True,
            "pt_adherence_pct": 95, "appointments_attended_pct": 100,
        },
        section_d={"readiness_0_10": 9},
    )


@pytest.mark.parametrize("day", [7, 14, 30])
def test_clean_answers_score_green(day):
    res = score_day_survey(day=day, answers=_clean_answers(), procedure_family="LEJR")
    assert res.tier == "GREEN"
    assert res.total_score >= 80
    assert res.day == day
    assert res.red_flags == []
    assert set(res.section_scores.keys()) == {"A", "B", "C", "D"}


def test_red_flag_chip_in_section_a_propagates():
    answers = _clean_answers()
    answers.section_a["chest_pain"] = True
    res = score_day_survey(day=7, answers=answers, procedure_family="LEJR")
    assert "CHEST_PAIN" in res.red_flags
    # Section A collapses to 0 on red-flag chip; total drops accordingly.
    assert res.section_scores["A"] == 0.0


def test_invalid_day_raises():
    with pytest.raises(ValueError):
        score_day_survey(day=5, answers=_clean_answers(), procedure_family="LEJR")


def test_lejr_uses_koos_section_b():
    """LEJR family selects KOOS_HOOS_JR PROM (function items)."""
    answers = _clean_answers()
    # If we zero out KOOS items, Section B should drop sharply.
    answers.section_b["stiffness"] = 0
    answers.section_b["pain"] = 0
    answers.section_b["function"] = 0
    answers.section_b["stairs"] = 0
    answers.section_b["rising"] = 0
    res = score_day_survey(day=7, answers=answers, procedure_family="LEJR")
    assert res.section_scores["B"] == 0.0


def test_spinal_uses_odi_inverted_disability():
    """ODI items at max disability → Section B = 0."""
    answers = _clean_answers()
    answers.section_b.update({
        "pain_intensity": 10, "personal_care": 10, "lifting": 10,
        "walking": 10, "sitting": 10, "standing": 10,
    })
    res = score_day_survey(day=14, answers=answers, procedure_family="SPINAL_FUSION")
    assert res.section_scores["B"] == 0.0


def test_cabg_uses_sf12_pcs_proxy():
    """Setting SF-12 PCS items to 0 → Section B = 0; LEJR-only items irrelevant."""
    answers = DayXSurveyAnswers(
        section_a=_clean_answers().section_a,
        section_b={
            "general_health": 0, "physical_function": 0, "role_physical": 0,
            "energy": 0, "social_function": 0,
        },
        section_c=_clean_answers().section_c,
        section_d=_clean_answers().section_d,
    )
    res = score_day_survey(day=30, answers=answers, procedure_family="CABG")
    assert res.section_scores["B"] == 0.0


def test_d7_red_threshold():
    """Day 7 RED requires total < 70."""
    answers = DayXSurveyAnswers(
        section_a={"pain_nrs": 9, "pain_interference": {"work": 5, "sleep": 5, "mood": 5, "enjoyment": 5}},
        section_b={"stiffness": 10, "pain": 10, "function": 10, "stairs": 10, "rising": 10},
        section_c={"remembered_to_take": False, "took_yesterday": False, "stopped_when_better": False,
                   "missed_when_traveling": False, "took_today": False,
                   "pt_adherence_pct": 0, "appointments_attended_pct": 0},
        section_d={"readiness_0_10": 1},
    )
    res = score_day_survey(day=7, answers=answers, procedure_family="LEJR")
    assert res.tier == "RED"
    assert res.total_score < 70


def test_d30_orange_threshold_is_more_lenient():
    """Day 30 ORANGE band: 65..79 (vs Day 7's 70..84)."""
    # Find a profile that lands at ~70 — should be GREEN at D30 (≥80? no), ORANGE at D7.
    answers = DayXSurveyAnswers(
        section_a={"pain_nrs": 4, "pain_interference": {"work": 2, "sleep": 2, "mood": 2, "enjoyment": 2}},
        section_b={"stiffness": 75, "pain": 75, "function": 75, "stairs": 75, "rising": 75},
        section_c={"remembered_to_take": True, "took_yesterday": True, "stopped_when_better": True,
                   "missed_when_traveling": True, "took_today": True,
                   "pt_adherence_pct": 70, "appointments_attended_pct": 75},
        section_d={"readiness_0_10": 6},
    )
    d7 = score_day_survey(day=7, answers=answers, procedure_family="LEJR")
    d30 = score_day_survey(day=30, answers=answers, procedure_family="LEJR")
    # Same answers should not be tiered worse at D30 than at D7 — D30
    # thresholds are intentionally more lenient.
    rank = {"GREEN": 1, "ORANGE": 2, "RED": 3}
    assert rank[d30.tier] <= rank[d7.tier]


def test_section_weights_sum_to_total():
    """Sanity: total = weighted average of section scores per day weights."""
    answers = _clean_answers()
    res = score_day_survey(day=14, answers=answers, procedure_family="LEJR")
    weights = {"A": 30, "B": 35, "C": 20, "D": 15}
    expected = sum(res.section_scores[s] * w for s, w in weights.items()) / 100.0
    assert abs(res.total_score - round(expected, 2)) <= 0.01
