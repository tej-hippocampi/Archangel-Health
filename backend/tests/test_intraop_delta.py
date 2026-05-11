"""
Unit tests for `compute_intraop_delta` (PRD §5.1).

Covers the seven worked examples in PRD §5.5 (A–G) plus the algorithmic
edge cases identified in the PRD: every per-family hard upgrade, OR-time
P90 ties, missing P90, two-soft aggregation boundary, contamination
class 3 vs 4, intra-op fracture as soft, idempotency, and procedural
abort.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.intraop import compute_intraop_delta  # noqa: E402
from triage.intraop.tuning import PROCEDURE_P90_MINUTES  # noqa: E402
from triage.intraop.types import HospitalProcedureStats, IntraopForm  # noqa: E402


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _stats() -> HospitalProcedureStats:
    return HospitalProcedureStats(or_duration_p90_minutes=PROCEDURE_P90_MINUTES)


def _form(**overrides) -> IntraopForm:
    """Build a default-quiet form (everything `False` / `0` / `'NO'`).
    Tests override only the fields under examination."""
    base = dict(
        documented_complication=False,
        ebl=0,
        transfusion_total_units=0,
        conversion="NO",
        sustained_hypotension=False,
        vasopressor_requirement="NONE",
        significant_arrhythmia=False,
        or_duration_minutes=60,
        difficult_airway=False,
        net_fluid_balance=0,
        anesthesia_type="GENERAL",
        hypoxia_event=False,
        procedural_aborted=False,
    )
    base.update(overrides)
    return IntraopForm(**base)


# ─── PRD §5.5 Examples A–G ───────────────────────────────────────────────────

def test_example_a_hard_complication_upgrades_to_t3():
    """Pre-op TIER_1; documented complication = vascular injury → TIER_3."""
    form = _form(
        documented_complication=True,
        complication_types=["VASCULAR_INJURY"],
    )
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.proposed_tier == "TIER_3"
    assert res.hard_upgrade_applied is True
    assert any(r.kind == "HARD" and r.code == "DOCUMENTED_COMPLICATION" for r in res.reasons)


def test_example_b_one_soft_eblbumps_one_step():
    """Pre-op TIER_1; EBL=600ml only → TIER_2 (1 soft, step up)."""
    form = _form(ebl=600)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.proposed_tier == "TIER_2"
    assert res.hard_upgrade_applied is False
    assert res.upgrade_steps == 1


def test_example_c_two_softs_aggregate_to_t3():
    """EBL=600 AND 3-unit transfusion → 2 softs aggregate → TIER_3."""
    form = _form(ebl=600, transfusion_total_units=3)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.proposed_tier == "TIER_3"
    assert res.hard_upgrade_applied is False
    assert res.upgrade_steps == 2


def test_example_d_t3_uneventful_stays_t3_via_resolve():
    """Pre-op TIER_3 + uneventful form → proposed_tier rolls forward as TIER_3
    (current passed in as TIER_3, no contributors fire)."""
    form = _form()
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_3")
    assert res.proposed_tier == "TIER_3"
    assert res.upgrade_steps == 0
    assert res.hard_upgrade_applied is False
    assert any(r.kind == "INFO" for r in res.reasons)


def test_example_e_spinal_dural_tear_is_hard():
    form = _form(dural_tear=True, number_of_levels_fused=1)
    res = compute_intraop_delta(form, "SPINAL_FUSION", _stats(), "TIER_2")
    assert res.proposed_tier == "TIER_3"
    assert res.hard_upgrade_applied is True
    assert any(r.code == "DURAL_TEAR" for r in res.reasons)


def test_example_f_bowel_class_3_one_soft_steps_to_t3():
    """Pre-op TIER_2 + contamination class 3 (1 soft) → step_up(TIER_2,1) = TIER_3."""
    form = _form(contamination_class=3)
    res = compute_intraop_delta(form, "MAJOR_BOWEL", _stats(), "TIER_2")
    assert res.proposed_tier == "TIER_3"
    assert res.upgrade_steps == 1
    assert any(r.code == "BOWEL_CONTAMINATION_CLASS_3" for r in res.reasons)


def test_example_g_or_time_over_p90_one_soft():
    """OR-time-over-P90 contributor: TIER_1 + LEJR with 130 min (P90=120) → TIER_2."""
    form = _form(or_duration_minutes=130)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.upgrade_steps == 1
    assert res.proposed_tier == "TIER_2"
    assert any(r.code == "OR_TIME_OVER_P90" for r in res.reasons)


# ─── Per-family hard upgrades ────────────────────────────────────────────────

def test_hard_bowel_class_4_dirty_infected():
    form = _form(contamination_class=4)
    res = compute_intraop_delta(form, "MAJOR_BOWEL", _stats(), "TIER_1")
    assert res.proposed_tier == "TIER_3"
    assert res.hard_upgrade_applied
    assert any(r.code == "CONTAMINATION_CLASS_4" for r in res.reasons)


def test_hard_cabg_required_mechanical_support():
    form = _form(weaning_from_bypass="REQUIRED_MECHANICAL_SUPPORT")
    res = compute_intraop_delta(form, "CABG", _stats(), "TIER_1")
    assert res.proposed_tier == "TIER_3"
    assert res.hard_upgrade_applied
    assert any(r.code == "REQUIRED_MECHANICAL_BYPASS_SUPPORT" for r in res.reasons)


def test_hard_procedure_aborted_any_family():
    form = _form(procedural_aborted=True, procedural_aborted_reason="Patient instability")
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.proposed_tier == "TIER_3"
    assert res.hard_upgrade_applied
    assert any(r.code == "PROCEDURE_ABORTED" for r in res.reasons)


def test_hard_dural_tear_only_for_spinal():
    """Dural tear flag set but family is LEJR → not a hard upgrade for non-spinal."""
    form = _form(dural_tear=True)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.hard_upgrade_applied is False
    assert all(r.code != "DURAL_TEAR" for r in res.reasons)


# ─── OR-time edge cases ──────────────────────────────────────────────────────

def test_or_time_exactly_p90_does_not_fire():
    """Strict greater-than (PRD edge case 10): OR=120 = P90 → no contributor."""
    form = _form(or_duration_minutes=120)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert res.upgrade_steps == 0
    assert res.proposed_tier == "TIER_1"


def test_or_time_missing_p90_falls_through_silently():
    """Missing family P90 → no OR-time contributor (no crash). Edge case 11."""
    stats = HospitalProcedureStats(or_duration_p90_minutes={})
    form = _form(or_duration_minutes=10000)
    res = compute_intraop_delta(form, "LEJR", stats, "TIER_1")
    assert all(r.code != "OR_TIME_OVER_P90" for r in res.reasons)


def test_or_time_no_family_no_contributor():
    """When procedure_family is None we cannot evaluate the per-family P90."""
    form = _form(or_duration_minutes=10000)
    res = compute_intraop_delta(form, None, _stats(), "TIER_1")
    assert all(r.code != "OR_TIME_OVER_P90" for r in res.reasons)


# ─── CABG-specific soft contributors ─────────────────────────────────────────

def test_cabg_cross_clamp_over_90_is_soft():
    form = _form(aortic_cross_clamp_minutes=95)
    res = compute_intraop_delta(form, "CABG", _stats(), "TIER_1")
    assert res.upgrade_steps == 1
    assert any(r.code == "CABG_CROSS_CLAMP_OVER_90" for r in res.reasons)


def test_cabg_cpb_over_120_is_soft():
    form = _form(cpb_time_minutes=130)
    res = compute_intraop_delta(form, "CABG", _stats(), "TIER_1")
    assert res.upgrade_steps == 1
    assert any(r.code == "CABG_CPB_OVER_120" for r in res.reasons)


def test_cabg_two_soft_aggregate_to_t3():
    form = _form(aortic_cross_clamp_minutes=95, cpb_time_minutes=130)
    res = compute_intraop_delta(form, "CABG", _stats(), "TIER_1")
    assert res.upgrade_steps == 2
    assert res.proposed_tier == "TIER_3"


# ─── Spinal soft contributors ────────────────────────────────────────────────

def test_spinal_4_levels_is_soft():
    form = _form(number_of_levels_fused=4)
    res = compute_intraop_delta(form, "SPINAL_FUSION", _stats(), "TIER_1")
    assert any(r.code == "SPINAL_FUSION_LEVELS_4_PLUS" for r in res.reasons)
    assert res.upgrade_steps == 1


def test_spinal_3_levels_does_not_fire():
    form = _form(number_of_levels_fused=3)
    res = compute_intraop_delta(form, "SPINAL_FUSION", _stats(), "TIER_1")
    assert all(r.code != "SPINAL_FUSION_LEVELS_4_PLUS" for r in res.reasons)


def test_spinal_neuromonitoring_changes_is_soft():
    form = _form(neuromonitoring_changes=True)
    res = compute_intraop_delta(form, "SPINAL_FUSION", _stats(), "TIER_1")
    assert any(r.code == "SPINAL_NEUROMONITORING_CHANGES" for r in res.reasons)


# ─── LEJR / Hip femur / bowel-specific ───────────────────────────────────────

def test_lejr_intraoperative_fracture_is_soft():
    form = _form(intraoperative_fracture=True, fracture_location="FEMORAL")
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert any(r.code == "LEJR_INTRAOPERATIVE_FRACTURE" for r in res.reasons)
    assert res.upgrade_steps == 1


def test_hip_femur_time_to_or_over_48_is_soft():
    form = _form(time_to_or_hours=49.0)
    res = compute_intraop_delta(form, "HIP_FEMUR_FRACTURE", _stats(), "TIER_1")
    assert any(r.code == "HIP_FEMUR_TIME_TO_OR_OVER_48" for r in res.reasons)


def test_hip_femur_time_to_or_exactly_48_does_not_fire():
    form = _form(time_to_or_hours=48.0)
    res = compute_intraop_delta(form, "HIP_FEMUR_FRACTURE", _stats(), "TIER_1")
    assert all(r.code != "HIP_FEMUR_TIME_TO_OR_OVER_48" for r in res.reasons)


def test_bowel_class_3_is_soft_class_4_is_hard():
    form3 = _form(contamination_class=3)
    res3 = compute_intraop_delta(form3, "MAJOR_BOWEL", _stats(), "TIER_1")
    assert any(r.code == "BOWEL_CONTAMINATION_CLASS_3" for r in res3.reasons)
    assert res3.hard_upgrade_applied is False

    form4 = _form(contamination_class=4)
    res4 = compute_intraop_delta(form4, "MAJOR_BOWEL", _stats(), "TIER_1")
    assert res4.hard_upgrade_applied is True


# ─── Universal physiology contributors ───────────────────────────────────────

def test_each_universal_soft_contributor_fires_once():
    """One-by-one: each universal soft contributor independently produces 1 step."""
    cases = [
        dict(ebl=501),
        dict(transfusion_total_units=2),
        dict(conversion="YES"),
        dict(sustained_hypotension=True),
        dict(vasopressor_requirement="SUSTAINED"),
        dict(hypoxia_event=True),
        dict(significant_arrhythmia=True),
        dict(difficult_airway=True),
    ]
    for c in cases:
        res = compute_intraop_delta(_form(**c), "LEJR", _stats(), "TIER_1")
        assert res.upgrade_steps == 1, c
        assert res.proposed_tier == "TIER_2", c


def test_transfusion_at_threshold_fires():
    """≥ 2 units fires (not strict)."""
    form = _form(transfusion_total_units=2)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert any(r.code == "TRANSFUSION_AT_OR_OVER_2" for r in res.reasons)


def test_transfusion_components_sum_when_aggregate_missing():
    """If aggregate is None, component sum drives the contributor."""
    form = IntraopForm(
        prbc_units=1, platelet_units=1, ffp_units=0, cryo_units=0,
        documented_complication=False, conversion="NO",
        sustained_hypotension=False, vasopressor_requirement="NONE",
        significant_arrhythmia=False, difficult_airway=False,
        anesthesia_type="GENERAL", procedural_aborted=False, hypoxia_event=False,
        ebl=0, or_duration_minutes=60, net_fluid_balance=0,
    )
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert any(r.code == "TRANSFUSION_AT_OR_OVER_2" for r in res.reasons)


def test_brief_vasopressor_does_not_fire_soft():
    form = _form(vasopressor_requirement="BRIEF")
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_1")
    assert all(r.code != "SUSTAINED_VASOPRESSOR" for r in res.reasons)
    assert res.upgrade_steps == 0


# ─── Tier ladder behavior ────────────────────────────────────────────────────

def test_t2_with_one_soft_steps_to_t3():
    """step_up from TIER_2 by 1 step is TIER_3."""
    form = _form(ebl=600)
    res = compute_intraop_delta(form, "LEJR", _stats(), "TIER_2")
    assert res.proposed_tier == "TIER_3"


def test_t3_with_no_factors_stays_t3():
    res = compute_intraop_delta(_form(), "LEJR", _stats(), "TIER_3")
    assert res.proposed_tier == "TIER_3"


# ─── Idempotency ─────────────────────────────────────────────────────────────

def test_compute_is_deterministic():
    form = _form(ebl=600, transfusion_total_units=3, neuromonitoring_changes=True)
    a = compute_intraop_delta(form, "SPINAL_FUSION", _stats(), "TIER_2").model_dump()
    b = compute_intraop_delta(form, "SPINAL_FUSION", _stats(), "TIER_2").model_dump()
    assert a == b


def test_model_and_tuning_versions_are_stamped():
    res = compute_intraop_delta(_form(), "LEJR", _stats(), "TIER_1")
    assert res.model_version == "intraop-delta@1.0.0"
    assert res.tuning_version == 1
