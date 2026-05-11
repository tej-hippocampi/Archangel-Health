"""
Daily symptom check-in scoring (PRD §4.2).

Implements the per-item weight matrix and tier mapping. Item-5 chips
(`incision_flags`) and item-8 chips (`red_flag_symptoms`) emit
*structured events* in addition to the score, which the post-op
re-tier consumes as soft / hard contributors.
"""

from __future__ import annotations

from typing import Any

from triage.postop.tuning import CHECKIN_CONFIG
from triage.postop.types import DailyCheckinAnswers, DailyCheckinScored


def _score_pain_nrs(nrs: int) -> float:
    """Item 1 — 100 minus NRS×10 → 0..100 (PRD §4.2)."""
    return max(0.0, min(100.0, 100.0 - float(nrs) * 10.0))


def _score_pain_trajectory(value: str) -> float:
    """Item 2 — Better=100 / Same=70 / Worse=20 (PRD §4.2)."""
    return {"BETTER": 100.0, "SAME": 70.0, "WORSE": 20.0}.get(value, 70.0)


def _score_fever(value: str) -> float:
    """Item 3 — No=100 / Yes(felt)=40 / Yes(measured)=0 (PRD §4.2)."""
    return {"NO": 100.0, "YES_FELT": 40.0, "YES_MEASURED": 0.0}.get(value, 100.0)


def _score_incision_change(value: str) -> float:
    """Item 4 — Better=100 / Same=85 / Worse=10 (PRD §4.2)."""
    return {"BETTER": 100.0, "SAME": 85.0, "WORSE": 10.0}.get(value, 85.0)


def _score_incision_flags(flags: list[str]) -> float:
    """Item 5 — None=100 / single=20 / multiple=0 (PRD §4.2)."""
    n = len(flags or [])
    if n == 0:
        return 100.0
    if n == 1:
        return 20.0
    return 0.0


def _score_nausea(value: str) -> float:
    """Item 6 (PRD §4.2)."""
    return {"NONE": 100.0, "MILD": 70.0, "MODERATE": 40.0, "SEVERE": 10.0}.get(value, 100.0)


def _score_eating(value: str) -> float:
    """Item 7 (PRD §4.2)."""
    return {"YES": 100.0, "SOME": 60.0, "ALMOST_NOTHING": 20.0}.get(value, 100.0)


def _score_red_flag_symptoms(flags: list[str]) -> float:
    """Item 8 — None=100 / any chip=0 (PRD §4.2)."""
    return 100.0 if not flags else 0.0


def _score_walking(value: str) -> float:
    """Item 9 (PRD §4.2)."""
    return {"YES": 100.0, "SOME": 60.0, "NO": 20.0}.get(value, 100.0)


def _score_worry(value: str) -> float:
    """Item 10 (PRD §4.2)."""
    return {
        "NOT_AT_ALL": 100.0, "A_LITTLE": 80.0, "MODERATELY": 50.0, "VERY": 20.0, "EXTREMELY": 0.0,
    }.get(value, 100.0)


def _tier_from_total(total: float, item5_count: int, item8_hit: bool) -> str:
    """PRD §4.2 tier mapping."""
    if total < 70 or item8_hit or item5_count >= 2:
        return "RED"
    if total < 85 or item5_count >= 1:
        return "ORANGE"
    return "GREEN"


def score_daily_checkin(answers: DailyCheckinAnswers) -> DailyCheckinScored:
    """Deterministic scorer for the 10-item daily symptom check-in (PRD §4.2).

    Returns a fully structured `DailyCheckinScored` payload — the apply
    layer reads `red_flags`, `wound_concern`, and `new_red_flag_symptom`
    to fire alerts and post-op re-tier contributors.
    """
    weights = CHECKIN_CONFIG["item_weights"]
    item_scores: dict[str, float] = {
        "pain_nrs":          _score_pain_nrs(answers.pain_nrs),
        "pain_trajectory":   _score_pain_trajectory(answers.pain_trajectory),
        "fever":             _score_fever(answers.fever),
        "incision_change":   _score_incision_change(answers.incision_change),
        "incision_flags":    _score_incision_flags(list(answers.incision_flags)),
        "nausea":            _score_nausea(answers.nausea),
        "eating_drinking":   _score_eating(answers.eating_drinking),
        "red_flag_symptoms": _score_red_flag_symptoms(list(answers.red_flag_symptoms)),
        "walking":           _score_walking(answers.walking),
        "worry_level":       _score_worry(answers.worry_level),
    }

    weighted_total = 0.0
    weight_sum = 0
    for k, w in weights.items():
        weighted_total += item_scores.get(k, 0.0) * float(w)
        weight_sum += int(w)

    raw_total = round(weighted_total / max(weight_sum, 1), 2)

    item5_count = len(answers.incision_flags or [])
    item8_hit = bool(answers.red_flag_symptoms)

    tier = _tier_from_total(raw_total, item5_count, item8_hit)

    red_flags: list[str] = list(answers.red_flag_symptoms or []) + list(answers.incision_flags or [])

    thresholds = CHECKIN_CONFIG["tier_thresholds"]
    if (
        tier in ("GREEN",)
        and (raw_total < thresholds["green_min"])
    ):
        # Defensive: if rounding produces e.g. 84.99 we want the threshold
        # comparison to apply consistently with `_tier_from_total`.
        tier = "ORANGE"

    return DailyCheckinScored(
        raw_total=raw_total,
        tier=tier,  # type: ignore[arg-type]
        red_flags=red_flags,
        new_red_flag_symptom=item8_hit,
        wound_concern=item5_count >= 1,
        pain_nrs=int(answers.pain_nrs),
        pain_trajectory=answers.pain_trajectory,
        item_scores=item_scores,
    )


def is_pain_above_expected_curve(*, episode_day: int, pain_nrs: int) -> bool:
    """Item 2 = Worse + item 1 above expected curve threshold for the
    episode-day (PRD §4.2 `PAIN_TRAJECTORY_ABNORMAL`)."""
    curve = CHECKIN_CONFIG["pain_expected_curve_floor"]
    floor = int(curve.get(int(episode_day), curve[max(curve.keys())]))
    return int(pain_nrs) > floor
