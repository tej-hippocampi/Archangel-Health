"""
Day 7 / Day 14 / Day 30 survey scoring (PRD §5).

Each survey is grouped into 4 sections (A: pain & symptoms, B: function,
C: engagement & adherence, D: recovery confidence). Each section emits a
0..100 sub-score; the total is the weighted sum per the day-specific
mapping in `tuning.SURVEY_CONFIG`.

Section B is procedure-family-specific (KOOS Jr / HOOS Jr for LEJR &
hip/femur fracture; ODI for spinal fusion; SF-12 PCS for CABG / major
bowel). Each PROM exposes a normalize-to-100 helper here so the scorer
stays a pure function of `DayXSurveyAnswers + procedure_family + day`.
"""

from __future__ import annotations

from typing import Any, Optional

from triage.postop.tuning import SURVEY_CONFIG
from triage.postop.types import (
    DayXSurveyAnswers,
    DayXSurveyScored,
    ProcedureFamily,
)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _clamp_pct(v: float) -> float:
    return max(0.0, min(100.0, float(v)))


def _coerce_num(value: Any, *, default: float = 0.0, lo: float = 0.0, hi: float = 10.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


# ─── Section A — pain & symptoms + red-flag screen (PRD §5.1) ────────────────


_RED_FLAG_KEYS = {
    "chest_pain", "severe_sob", "calf_swelling", "severe_bleeding",
    "mental_status_change", "fainting", "severe_headache",
    "sudden_weakness_one_side",
}


def _score_section_a(payload: dict[str, Any]) -> tuple[float, list[str]]:
    """Section A — average of NRS-derived score and 4 PROMIS-aligned
    pain-interference items, then dropped to 0 on any red-flag chip."""
    nrs = _coerce_num(payload.get("pain_nrs", 0), lo=0.0, hi=10.0)
    nrs_score = _clamp_pct(100.0 - nrs * 10.0)

    interference = payload.get("pain_interference") or {}
    # PROMIS pain interference items are 1..5 with 5 = most interference.
    items = ["work", "sleep", "mood", "enjoyment"]
    raw = [
        _coerce_num(interference.get(k, 1), lo=1.0, hi=5.0) for k in items
    ]
    interference_avg = sum(raw) / max(len(raw), 1)
    interference_score = _clamp_pct(100.0 * (1.0 - (interference_avg - 1.0) / 4.0))

    section_a = round((nrs_score + interference_score) / 2.0, 2)

    red_flags: list[str] = []
    for k in _RED_FLAG_KEYS:
        if bool(payload.get(k)):
            red_flags.append(k.upper())

    if red_flags:
        # Section A score collapses to 0 when any red-flag chip is hit
        # (PRD §5.3 — same propagation rule as the daily check-in).
        section_a = 0.0

    return section_a, red_flags


# ─── Section B — function, procedure-family-specific (PRD §5.1 / §15) ────────


def _score_section_b(
    payload: dict[str, Any], procedure_family: Optional[ProcedureFamily]
) -> float:
    """Procedure-family-specific function score normalized to 0..100.

    Each PROM is scored with its standard direction (higher = better).
    Patient submits on a uniform 0..100 self-report scale; the scorer
    normalizes only to keep the scale consistent regardless of the
    underlying instrument.
    """
    fam = procedure_family or "LEJR"
    proms = SURVEY_CONFIG["section_b_proms"]
    prom_id = proms.get(fam, "SF12_PCS")

    if prom_id == "KOOS_HOOS_JR":
        # Abridged KOOS Jr / HOOS Jr (PRD §5.1) — patient submits 5–8 items
        # each scaled 0..100 (higher = better).
        items = [
            _coerce_num(payload.get(k, 50), lo=0.0, hi=100.0)
            for k in ("stiffness", "pain", "function", "stairs", "rising")
        ]
        return round(sum(items) / max(len(items), 1), 2)
    if prom_id == "ODI":
        # Oswestry Disability Index — patient submits 0..10 disability
        # score per item; higher = more disability. Invert.
        items = [
            _coerce_num(payload.get(k, 5), lo=0.0, hi=10.0)
            for k in ("pain_intensity", "personal_care", "lifting", "walking", "sitting", "standing")
        ]
        avg = sum(items) / max(len(items), 1)
        return round(_clamp_pct(100.0 * (1.0 - avg / 10.0)), 2)
    # SF-12 PCS proxy — patient submits 5 items each scaled 0..100.
    items = [
        _coerce_num(payload.get(k, 50), lo=0.0, hi=100.0)
        for k in ("general_health", "physical_function", "role_physical", "energy", "social_function")
    ]
    return round(sum(items) / max(len(items), 1), 2)


# ─── Section C — engagement & adherence (PRD §5.1) ───────────────────────────


def _score_section_c(payload: dict[str, Any]) -> float:
    """Med adherence (8-item Morisky-style proxy) + PT adherence + appointments."""
    # Morisky-style proxy — boolean items where True = adherent.
    morisky_keys = [
        "remembered_to_take", "took_yesterday", "stopped_when_better",
        "missed_when_traveling", "took_today",
    ]
    answers = [bool(payload.get(k, True)) for k in morisky_keys]
    morisky_score = (sum(1 for a in answers if a) / max(len(answers), 1)) * 100.0

    pt_score = _coerce_num(payload.get("pt_adherence_pct", 80.0), lo=0.0, hi=100.0)
    appts_attended = _coerce_num(payload.get("appointments_attended_pct", 80.0), lo=0.0, hi=100.0)

    return round(_clamp_pct(0.5 * morisky_score + 0.3 * pt_score + 0.2 * appts_attended), 2)


# ─── Section D — recovery confidence (PRD §5.1) ──────────────────────────────


def _score_section_d(payload: dict[str, Any]) -> float:
    """Single 0..10 readiness item rescaled to 0..100."""
    readiness = _coerce_num(payload.get("readiness_0_10", 7), lo=0.0, hi=10.0)
    return round(_clamp_pct(readiness * 10.0), 2)


# ─── Public API ──────────────────────────────────────────────────────────────


def _tier_from_total(day: int, total: float) -> str:
    bands = SURVEY_CONFIG["tier_thresholds"][day]
    if total >= bands["green_min"]:
        return "GREEN"
    if total >= bands["orange_min"]:
        return "ORANGE"
    return "RED"


def score_day_survey(
    *,
    day: int,
    answers: DayXSurveyAnswers,
    procedure_family: Optional[ProcedureFamily] = None,
) -> DayXSurveyScored:
    """Deterministic scorer for the D7 / D14 / D30 surveys (PRD §5.2)."""
    if int(day) not in (7, 14, 30):
        raise ValueError(f"day must be 7, 14, or 30 (got {day})")

    section_a, red_flags = _score_section_a(answers.section_a or {})
    section_b = _score_section_b(answers.section_b or {}, procedure_family)
    section_c = _score_section_c(answers.section_c or {})
    section_d = _score_section_d(answers.section_d or {})

    weights = SURVEY_CONFIG["section_weights"][int(day)]
    weighted = (
        section_a * weights["A"]
        + section_b * weights["B"]
        + section_c * weights["C"]
        + section_d * weights["D"]
    )
    weight_sum = sum(weights.values())
    total = round(weighted / max(weight_sum, 1), 2)

    tier = _tier_from_total(int(day), total)

    return DayXSurveyScored(
        day=int(day),
        section_scores={"A": section_a, "B": section_b, "C": section_c, "D": section_d},
        total_score=total,
        tier=tier,  # type: ignore[arg-type]
        red_flags=red_flags,
        procedure_family=procedure_family,
    )
