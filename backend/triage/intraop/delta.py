"""
Intra-Op delta computation (PRD §5.1).

Pure function — consumes a locked form snapshot and the patient's current
tier, emits a `proposed_tier` plus itemized reasons. The episode's final
tier is then resolved upstream by `resolve_final_tier(current, proposed)`.

Algorithm sketch:
  1. Hard upgrades (any one ⇒ TIER_3, no further evaluation needed).
  2. Soft upgrades (universal + procedure-family-specific) — each adds one step.
  3. Map the upgrade count onto a tier delta:
       hard               → TIER_3
       steps ≥ 2          → TIER_3 (aggregate)
       steps == 1         → step_up(current, 1)
       steps == 0         → current (with INFO reason)
"""

from __future__ import annotations

from typing import Optional

from triage.intraop.tuning import (
    HARD_LABELS,
    MODEL_VERSION,
    SOFT_LABELS,
    SOFT_THRESHOLDS,
    TUNING_VERSION,
    step_up,
)
from triage.intraop.types import (
    HospitalProcedureStats,
    IntraopDeltaResult,
    IntraopForm,
    IntraopReason,
)
from triage.types import ProcedureFamily, Tier


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _hard(code: str, detail: Optional[str] = None) -> IntraopReason:
    return IntraopReason(kind="HARD", code=code, label=HARD_LABELS.get(code, code), detail=detail)


def _soft(code: str, detail: Optional[str] = None) -> IntraopReason:
    return IntraopReason(kind="SOFT", code=code, label=SOFT_LABELS.get(code, code), detail=detail)


def _info(label: str) -> IntraopReason:
    return IntraopReason(kind="INFO", code="NO_INTRAOP_RISK_FACTORS", label=label)


def _total_transfusion_units(form: IntraopForm) -> int:
    """Prefer the aggregate; fall back to the component sum if it is set."""
    if form.transfusion_total_units is not None:
        return form.transfusion_total_units
    return sum(
        v or 0 for v in (
            form.prbc_units, form.platelet_units, form.ffp_units, form.cryo_units,
        )
    )


# ─── Hard upgrades (PRD §5.1) ────────────────────────────────────────────────

def _evaluate_hard_upgrades(
    form: IntraopForm,
    family: Optional[ProcedureFamily],
) -> list[IntraopReason]:
    """Return all hard upgrades that fire. The caller treats *any one*
    as enough to set the proposed tier to TIER_3 — but we still record
    every fired hard reason for audit clarity."""
    reasons: list[IntraopReason] = []

    if form.documented_complication is True:
        types = ", ".join(form.complication_types or []) or None
        reasons.append(_hard("DOCUMENTED_COMPLICATION", types))

    if family == "SPINAL_FUSION" and form.dural_tear is True:
        reasons.append(_hard("DURAL_TEAR"))

    if family == "MAJOR_BOWEL" and form.contamination_class == 4:
        reasons.append(_hard("CONTAMINATION_CLASS_4"))

    if family == "CABG" and form.weaning_from_bypass == "REQUIRED_MECHANICAL_SUPPORT":
        reasons.append(_hard("REQUIRED_MECHANICAL_BYPASS_SUPPORT"))

    if form.procedural_aborted is True:
        detail = form.procedural_aborted_reason or None
        reasons.append(_hard("PROCEDURE_ABORTED", detail))

    return reasons


# ─── Soft upgrades (PRD §5.1) ────────────────────────────────────────────────

def _evaluate_soft_upgrades(
    form: IntraopForm,
    family: Optional[ProcedureFamily],
    stats: HospitalProcedureStats,
) -> list[IntraopReason]:
    """Each fired soft contributor adds one step. Strict greater-than
    semantics on the numeric thresholds (PRD edge case 10).

    NOTE — we deliberately do *not* fire soft contributors that overlap
    with already-fired hard upgrades:
      - `BOWEL_CONTAMINATION_CLASS_3` only fires when class != 4.
      - `LEJR_INTRAOPERATIVE_FRACTURE` always fires when set, since
        intra-op fracture is *not* a hard escalator (only the per-family
        hard list above is).
    """
    reasons: list[IntraopReason] = []

    # ─── Universal physiology / events ────────────────────────────────────
    ebl_threshold = SOFT_THRESHOLDS["ebl_ml"]
    if form.ebl is not None and form.ebl > ebl_threshold:
        reasons.append(_soft("EBL_OVER_THRESHOLD", f"EBL {form.ebl} ml exceeds {ebl_threshold} ml threshold"))

    txn_threshold = SOFT_THRESHOLDS["transfusion_units"]
    txn_total = _total_transfusion_units(form)
    if txn_total >= txn_threshold:
        reasons.append(_soft("TRANSFUSION_AT_OR_OVER_2", f"Transfused {txn_total} total units"))

    if form.conversion == "YES":
        reasons.append(_soft("CONVERSION_MIS_TO_OPEN"))

    if form.sustained_hypotension is True:
        reasons.append(_soft("SUSTAINED_HYPOTENSION"))

    if form.vasopressor_requirement == "SUSTAINED":
        reasons.append(_soft("SUSTAINED_VASOPRESSOR"))

    if form.hypoxia_event is True:
        reasons.append(_soft("HYPOXIA_EVENT"))

    if form.significant_arrhythmia is True:
        reasons.append(_soft("SIGNIFICANT_ARRHYTHMIA"))

    if form.difficult_airway is True:
        reasons.append(_soft("DIFFICULT_AIRWAY"))

    # OR time vs hospital P90 (strict >; PRD edge case 10).
    if family is not None and form.or_duration_minutes is not None:
        p90 = stats.or_duration_p90_minutes.get(family)
        if p90 is not None and form.or_duration_minutes > p90:
            reasons.append(_soft(
                "OR_TIME_OVER_P90",
                f"OR time {form.or_duration_minutes} min exceeds P90 ({p90} min) for {family}",
            ))

    # ─── Procedure-family-specific ────────────────────────────────────────
    if family == "CABG":
        cc_threshold = SOFT_THRESHOLDS["cabg_cross_clamp_minutes"]
        if form.aortic_cross_clamp_minutes is not None and form.aortic_cross_clamp_minutes > cc_threshold:
            reasons.append(_soft(
                "CABG_CROSS_CLAMP_OVER_90",
                f"Cross-clamp time {form.aortic_cross_clamp_minutes} min",
            ))
        cpb_threshold = SOFT_THRESHOLDS["cabg_cpb_minutes"]
        if form.cpb_time_minutes is not None and form.cpb_time_minutes > cpb_threshold:
            reasons.append(_soft(
                "CABG_CPB_OVER_120",
                f"CPB time {form.cpb_time_minutes} min",
            ))

    if family == "SPINAL_FUSION":
        levels_threshold = SOFT_THRESHOLDS["spinal_levels_aggregate"]
        if form.number_of_levels_fused is not None and form.number_of_levels_fused >= levels_threshold:
            reasons.append(_soft(
                "SPINAL_FUSION_LEVELS_4_PLUS",
                f"{form.number_of_levels_fused}-level fusion",
            ))
        if form.neuromonitoring_changes is True:
            reasons.append(_soft("SPINAL_NEUROMONITORING_CHANGES"))

    if family == "LEJR" and form.intraoperative_fracture is True:
        detail = form.fracture_location or None
        reasons.append(_soft("LEJR_INTRAOPERATIVE_FRACTURE", detail))

    # Class-3 contamination is soft; class-4 is hard (handled above).
    if family == "MAJOR_BOWEL" and form.contamination_class == 3:
        reasons.append(_soft("BOWEL_CONTAMINATION_CLASS_3"))

    if family == "HIP_FEMUR_FRACTURE":
        time_threshold = SOFT_THRESHOLDS["hip_femur_time_to_or_hours"]
        if form.time_to_or_hours is not None and form.time_to_or_hours > time_threshold:
            reasons.append(_soft(
                "HIP_FEMUR_TIME_TO_OR_OVER_48",
                f"Time-to-OR {form.time_to_or_hours} h exceeds {time_threshold} h threshold",
            ))

    return reasons


# ─── Top-level ───────────────────────────────────────────────────────────────

def compute_intraop_delta(
    form: IntraopForm,
    procedure_family: Optional[ProcedureFamily],
    hospital_stats: HospitalProcedureStats,
    current_tier: Tier,
) -> IntraopDeltaResult:
    """Compute the proposed intra-op tier per PRD §5.1.

    The function is pure and deterministic; it does not write tier
    state. The caller (`apply_intraop_reassessment`) resolves the final
    tier with `resolve_final_tier(current_tier, proposed_tier)`.
    """
    hard_reasons = _evaluate_hard_upgrades(form, procedure_family)
    soft_reasons = _evaluate_soft_upgrades(form, procedure_family, hospital_stats)

    reasons = list(hard_reasons) + list(soft_reasons)

    if hard_reasons:
        return IntraopDeltaResult(
            proposed_tier="TIER_3",
            hard_upgrade_applied=True,
            upgrade_steps=0,
            reasons=reasons,
            model_version=MODEL_VERSION,
            tuning_version=TUNING_VERSION,
        )

    steps = len(soft_reasons)
    if steps >= 2:
        proposed: Tier = "TIER_3"
    elif steps == 1:
        proposed = step_up(current_tier, 1)  # type: ignore[assignment]
    else:
        proposed = current_tier
        reasons.append(_info("No intra-operative risk factors identified"))

    return IntraopDeltaResult(
        proposed_tier=proposed,
        hard_upgrade_applied=False,
        upgrade_steps=steps,
        reasons=reasons,
        model_version=MODEL_VERSION,
        tuning_version=TUNING_VERSION,
    )
