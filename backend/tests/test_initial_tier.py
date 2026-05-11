"""
Unit tests for the Initial Pre-Op Triage algorithm.

Covers the worked examples from PRD §5.6 (A–E) plus boundary cases for
`score_to_tier`. Each example asserts the resulting tier, the score (or
None for hard escalators), and the codes of the contributing reasons so
regressions in any flag deriver fail loudly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage import assign_initial_tier, score_to_tier  # noqa: E402
from triage.types import (  # noqa: E402
    ActiveProblem,
    ActiveProblemsInput,
    Allergy,
    AllergiesInput,
    InitialTierInput,
    LabResult,
    Medication,
    MedicationsInput,
    ProcedureInput,
    RecentLabsInput,
    SocialHistoryInput,
    StudyResult,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _proc(family: str, *, emergency: bool = False, sex_note: str = "") -> ProcedureInput:
    return ProcedureInput(
        cpt_code="00000",
        anchor_procedure_family=family,  # type: ignore[arg-type]
        scheduled_date="2026-06-01",
        is_emergency=emergency,
        notes=sex_note,
    )


def _problems(*codes: str, functional: str = "INDEPENDENT", bmi: float | None = None) -> ActiveProblemsInput:
    return ActiveProblemsInput(
        problems=[ActiveProblem(icd10=c, description=c, status="ACTIVE") for c in codes],
        functional_status=functional,  # type: ignore[arg-type]
        bmi=bmi,
    )


def _meds(*names: str, count_pad: int = 0) -> MedicationsInput:
    meds = [Medication(name=n) for n in names]
    # Pad with anonymous meds to hit polypharmacy thresholds when needed.
    meds.extend(Medication(name=f"Filler-{i}") for i in range(count_pad))
    return MedicationsInput(medications=meds)


def _labs(**kvs: float) -> RecentLabsInput:
    """Build a labs input from name=value kwargs (e.g. _labs(Hemoglobin=11.8))."""
    items = [
        LabResult(name=name, value=float(val), unit="", drawn_at="2026-05-15")
        for name, val in kvs.items()
    ]
    return RecentLabsInput(labs=items)


def _social(
    *,
    age: int = 50,
    smoking: str = "NEVER",
    pack_years: float | None = None,
    lives_alone: bool | None = False,
    has_caregiver: bool | None = True,
    housing: str = "STABLE",
    food: str = "SECURE",
) -> SocialHistoryInput:
    return SocialHistoryInput(
        smoking_status=smoking,  # type: ignore[arg-type]
        pack_years=pack_years,
        lives_alone=lives_alone,
        has_reliable_caregiver=has_caregiver,
        housing_status=housing,  # type: ignore[arg-type]
        food_security=food,  # type: ignore[arg-type]
        age=age,
    )


# ─── Score → tier boundary tests (§5.3) ──────────────────────────────────────

def test_score_to_tier_boundaries():
    assert score_to_tier(0) == "TIER_1"
    assert score_to_tier(3) == "TIER_1"
    assert score_to_tier(4) == "TIER_2"
    assert score_to_tier(7) == "TIER_2"
    assert score_to_tier(8) == "TIER_3"
    assert score_to_tier(99) == "TIER_3"


# ─── Example A — Tier 1 ──────────────────────────────────────────────────────

def test_example_a_tier_1():
    """62-yo M elective TKA + HTN on lisinopril → score 1 → TIER_1."""
    inp = InitialTierInput(
        procedure=_proc("LEJR"),
        active_problems=_problems("I10"),
        medications=_meds("lisinopril"),
        allergies=AllergiesInput(),
        social_history=_social(age=62, smoking="NEVER"),
        recent_labs=_labs(Hemoglobin=14.2, eGFR=78, HbA1c=5.6, Albumin=4.1),
    )
    out = assign_initial_tier(inp)
    assert out.tier == "TIER_1"
    assert out.score == 1
    soft_codes = {r.code for r in out.reasons if r.kind == "SOFT"}
    assert soft_codes == {"HTN_REQUIRING_MEDS"}


# ─── Example B — Tier 3 via score ────────────────────────────────────────────

def test_example_b_tier_3_via_score():
    """71-yo F THA with DM-on-insulin, CAD, OSA, polypharmacy, anemia, eGFR 56.

    PRD calls this out as the model working correctly: the patient is high-risk
    and lands TIER_3 even without a hard escalator. Expected contributors:
      DIABETES_INSULIN_DEPENDENT (+2), CAD (+2), OBSTRUCTIVE_SLEEP_APNEA (+1),
      POLYPHARMACY_HIGH (+1), ANEMIA_PREOP (+1), RENAL_IMPAIRMENT (+2)
      = 9 → TIER_3.
    """
    inp = InitialTierInput(
        procedure=_proc("LEJR", sex_note="71-year-old female THA"),
        active_problems=_problems("E11", "I25", "G47.33"),
        medications=_meds(
            "insulin glargine",       # → DIABETES_INSULIN_DEPENDENT
            "metformin",
            "aspirin",
            "atorvastatin",
            "metoprolol",
            "lisinopril",             # → HTN_REQUIRING_MEDS suppressed (no HTN code in problems)
            "sertraline",
            "gabapentin",
            "vitamin D",
            "omeprazole",
        ),                            # 10 meds → POLYPHARMACY_HIGH
        social_history=_social(age=71, smoking="FORMER"),
        recent_labs=_labs(Hemoglobin=11.8, eGFR=56, HbA1c=7.4, Albumin=3.8),
    )
    out = assign_initial_tier(inp)
    assert out.tier == "TIER_3"
    assert out.score == 9

    soft_codes = {r.code for r in out.reasons if r.kind == "SOFT"}
    expected_contributors = {
        "DIABETES_INSULIN_DEPENDENT",
        "CAD",
        "OBSTRUCTIVE_SLEEP_APNEA",
        "POLYPHARMACY_HIGH",
        "ANEMIA_PREOP",
        "RENAL_IMPAIRMENT",
    }
    assert expected_contributors.issubset(soft_codes), \
        f"missing contributors: {expected_contributors - soft_codes}"


# ─── Example C — Tier 2 ──────────────────────────────────────────────────────

def test_example_c_tier_2_clean():
    """58-yo M sigmoid colectomy + HTN → procedure base 3 + HTN 1 = 4 → TIER_2.

    Lives alone but has a reliable caregiver — must NOT trigger the
    LIVES_ALONE_NO_CAREGIVER hard escalator.
    """
    inp = InitialTierInput(
        procedure=_proc("MAJOR_BOWEL"),
        active_problems=_problems("I10"),
        medications=_meds("lisinopril", "aspirin"),
        allergies=AllergiesInput(),
        social_history=_social(
            age=58, lives_alone=True, has_caregiver=True,
        ),
        recent_labs=_labs(Hemoglobin=14.0, eGFR=85, Albumin=4.2),
    )
    out = assign_initial_tier(inp)
    assert out.tier == "TIER_2"
    assert out.score == 4
    base_reason = next((r for r in out.reasons if r.kind == "BASE"), None)
    assert base_reason is not None and base_reason.weight == 3
    soft_codes = {r.code for r in out.reasons if r.kind == "SOFT"}
    assert "HTN_REQUIRING_MEDS" in soft_codes
    assert "LIVES_ALONE_NO_CAREGIVER" not in {r.code for r in out.reasons}


# ─── Example D — Tier 3 hard (emergency) ─────────────────────────────────────

def test_example_d_tier_3_hard_emergency():
    """68-yo M emergent open repair of perforated diverticulitis."""
    inp = InitialTierInput(
        procedure=_proc("MAJOR_BOWEL", emergency=True),
        active_problems=_problems(),
        medications=_meds(),
        social_history=_social(age=68),
        recent_labs=RecentLabsInput(),
    )
    out = assign_initial_tier(inp)
    assert out.tier == "TIER_3"
    assert out.score is None
    assert len(out.reasons) == 1
    assert out.reasons[0].kind == "HARD"
    assert out.reasons[0].code == "EMERGENCY_CASE"


# ─── Example E — Tier 3 hard (social) ────────────────────────────────────────

def test_example_e_tier_3_hard_social():
    """64-yo F TKA, no comorbidities, lives alone w/ no reliable caregiver."""
    inp = InitialTierInput(
        procedure=_proc("LEJR"),
        active_problems=_problems(),
        medications=_meds(),
        social_history=_social(
            age=64, lives_alone=True, has_caregiver=False,
        ),
        recent_labs=RecentLabsInput(),
    )
    out = assign_initial_tier(inp)
    assert out.tier == "TIER_3"
    assert out.score is None
    assert out.reasons[0].kind == "HARD"
    assert out.reasons[0].code == "LIVES_ALONE_NO_CAREGIVER"


# ─── Edge case — anticoag suppresses COAGULOPATHY (PRD §12.6) ────────────────

def test_anticoagulation_suppresses_coagulopathy():
    """Therapeutic anticoagulation + INR 2.5 should NOT fire COAGULOPATHY."""
    inp = InitialTierInput(
        procedure=_proc("LEJR"),
        active_problems=_problems("I10"),
        medications=_meds("warfarin", "lisinopril"),
        social_history=_social(age=70),
        recent_labs=_labs(INR=2.5, Hemoglobin=13.0, eGFR=80, Albumin=4.0),
    )
    out = assign_initial_tier(inp)
    soft_codes = {r.code for r in out.reasons if r.kind == "SOFT"}
    assert "ANTICOAGULANT_THERAPEUTIC" in soft_codes
    assert "COAGULOPATHY" not in soft_codes


# ─── Edge case — totally dependent functional status is hard ─────────────────

def test_functional_totally_dependent_is_hard():
    inp = InitialTierInput(
        procedure=_proc("LEJR"),
        active_problems=_problems("I10", functional="TOTALLY_DEPENDENT"),
        medications=_meds(),
        social_history=_social(age=78),
        recent_labs=RecentLabsInput(),
    )
    out = assign_initial_tier(inp)
    assert out.tier == "TIER_3"
    assert out.score is None
    assert out.reasons[0].code == "FUNCTIONAL_TOTALLY_DEPENDENT"


# ─── Sanity — reasons list sums to displayed score ───────────────────────────

def test_reasons_sum_equals_score():
    """AC-6.3: every contributing flag must be enumerated and weights sum to total."""
    inp = InitialTierInput(
        procedure=_proc("MAJOR_BOWEL"),
        active_problems=_problems("I10"),
        medications=_meds("lisinopril"),
        social_history=_social(age=60),
        recent_labs=RecentLabsInput(),
    )
    out = assign_initial_tier(inp)
    total = sum((r.weight or 0) for r in out.reasons)
    assert total == out.score
