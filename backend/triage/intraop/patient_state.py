"""
Helpers for the intra-op-relevant fields on the in-memory patient blob.

The CareGuide app stores patient records in a process-local dict
(`app.state.patient_store`). The intra-op feature needs four extra
keys per patient:

    phase                    : "pre_op" | "intra_op" | "post_op"
    or_started_at            : ISO timestamp (optional)
    or_ended_at              : ISO timestamp (optional; gates Switch-to-post-op)
    current_tier             : "TIER_1" | "TIER_2" | "TIER_3"
    current_tier_was_hard    : bool   (sticky-hard guard inheritance)
    anchor_procedure_family  : ProcedureFamily

This module provides idempotent ensure / read / write helpers so the
keys default consistently regardless of which path first touches a
patient. The fallbacks below are deliberately conservative:

    phase                   = "pre_op"
    current_tier            = "TIER_1"
    anchor_procedure_family = "LEJR"   (most common in-repo demo family)
"""

from __future__ import annotations

from typing import Any, Optional

from triage.types import ProcedureFamily, Tier


_PHASES = {"pre_op", "intra_op", "post_op"}
_FAMILY_BY_KEYWORD: list[tuple[tuple[str, ...], ProcedureFamily]] = [
    (("knee", "hip replac", "arthroplasty", "lejr", "tka", "tha"), "LEJR"),
    (("cabg", "coronary artery bypass"), "CABG"),
    (("spinal fusion", "spine fusion", "lumbar fusion", "cervical fusion"), "SPINAL_FUSION"),
    (("hip fracture", "femur fracture", "femoral neck"), "HIP_FEMUR_FRACTURE"),
    (("colectomy", "bowel resection", "small bowel"), "MAJOR_BOWEL"),
]


def _infer_family(procedure_name: Optional[str]) -> ProcedureFamily:
    name = (procedure_name or "").lower()
    for keywords, family in _FAMILY_BY_KEYWORD:
        if any(k in name for k in keywords):
            return family
    return "LEJR"


def ensure_intraop_patient_state(patient: dict) -> dict:
    """Mutate the patient blob in place (and return it) so it has the
    intra-op fields we depend on. Idempotent."""
    sd = patient.get("structured_data") or {}

    if patient.get("phase") not in _PHASES:
        patient["phase"] = "pre_op"

    if patient.get("current_tier") not in ("TIER_1", "TIER_2", "TIER_3"):
        patient["current_tier"] = "TIER_1"

    patient.setdefault("current_tier_was_hard", False)
    patient.setdefault("or_started_at", None)
    patient.setdefault("or_ended_at", None)

    if not patient.get("anchor_procedure_family"):
        # Allow callers to pre-set this; otherwise infer from procedure name.
        patient["anchor_procedure_family"] = _infer_family(sd.get("procedure_name"))

    return patient


def get_current_tier(patient: dict) -> Tier:
    ensure_intraop_patient_state(patient)
    return patient["current_tier"]   # type: ignore[return-value]


def get_anchor_procedure_family(patient: dict) -> ProcedureFamily:
    ensure_intraop_patient_state(patient)
    return patient["anchor_procedure_family"]   # type: ignore[return-value]


def set_current_tier(patient: dict, tier: Tier, *, was_hard: Optional[bool] = None) -> None:
    ensure_intraop_patient_state(patient)
    patient["current_tier"] = tier
    if was_hard is not None:
        patient["current_tier_was_hard"] = bool(was_hard)


def set_phase(patient: dict, phase: str) -> None:
    if phase not in _PHASES:
        raise ValueError(f"unknown phase: {phase}")
    ensure_intraop_patient_state(patient)
    patient["phase"] = phase


def set_or_ended_at(patient: dict, ts_iso: str) -> None:
    ensure_intraop_patient_state(patient)
    patient["or_ended_at"] = ts_iso
    if not patient.get("phase") or patient["phase"] == "pre_op":
        patient["phase"] = "intra_op"


def set_or_started_at(patient: dict, ts_iso: str) -> None:
    ensure_intraop_patient_state(patient)
    patient["or_started_at"] = ts_iso


def to_public(patient: dict) -> dict[str, Any]:
    """Snapshot of the intra-op-relevant subset for API responses."""
    ensure_intraop_patient_state(patient)
    return {
        "phase":                   patient["phase"],
        "currentTier":             patient["current_tier"],
        "currentTierWasHard":      bool(patient.get("current_tier_was_hard")),
        "orStartedAt":             patient.get("or_started_at"),
        "orEndedAt":               patient.get("or_ended_at"),
        "anchorProcedureFamily":   patient.get("anchor_procedure_family"),
    }
